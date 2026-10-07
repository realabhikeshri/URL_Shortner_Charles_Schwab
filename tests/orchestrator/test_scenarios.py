"""End-to-end: the three scenarios through the real engine, agents, policies and gates.

These are the strongest evidence that the orchestrator works: they build the service from an empty
workspace, evolve it, and gate every step with lint, tests, coverage, typecheck and policies.
"""

import asyncio
from pathlib import Path

import pytest

from orchestrator import cli, runner
from orchestrator.audit import verify_chain
from orchestrator.models import ApprovalStatus, RunState
from orchestrator.state import StateStore

pytestmark = pytest.mark.slow


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(cli, "WORK", tmp_path / "work")
    monkeypatch.setenv("ORCH_APPROVER", "test-reviewer@example.com")
    return tmp_path


def metrics(run_dir: Path) -> dict[str, object]:
    return runner.run_metrics_summary(run_dir)


def events(run_dir: Path) -> list[str]:
    from orchestrator.audit import read_events

    return [e["type"] for e in read_events(run_dir / "audit.jsonl")]


def test_greenfield_then_brownfield_then_ambiguous(sandbox: Path) -> None:
    ws = sandbox / "work" / "demo"
    runner.init_scaffold(ws)

    # ---------------------------------------------------------------- greenfield
    gf_dir, gf_state, _ = cli.run_scenario(
        "greenfield",
        workspace=ws,
        auto_approve="low,medium",
        high_risk=False,
        interactive=False,
        provider="deterministic",
    )
    assert gf_state is RunState.SUCCEEDED
    gf = metrics(gf_dir)
    assert gf["nodes_total"] == 14
    assert gf["audit_chain_ok"]
    assert (ws / "service" / "app.py").exists()
    assert (gf_dir / "report.html").exists()
    assert "plan_expanded" in events(gf_dir)

    # ---------------------------------------------------------------- brownfield
    bf_dir, bf_state, _ = cli.run_scenario(
        "brownfield",
        workspace=ws,
        auto_approve="low,medium",
        high_risk=False,
        interactive=False,
        provider="deterministic",
    )
    assert bf_state is RunState.SUCCEEDED
    bf = metrics(bf_dir)
    assert bf["retries"] == 1  # the deliberately naive alias fix
    assert bf["rollbacks"] == 1
    assert bf["recoveries"] == 1
    store = StateStore(bf_dir / "state.db")
    gates = {(g["node_id"], g["gate"], g["attempt"]): g["passed"] for g in store.gate_results()}
    assert gates[("test.regression_alias", "tests_fail_first", 1)] is True
    assert gates[("implement.alias_fix", "targeted_tests", 1)] is False
    assert gates[("implement.alias_fix", "targeted_tests", 2)] is True
    approvals = {a.node_id: a for a in store.approvals()}
    assert "db_migration" in approvals["implement.migration_0002"].reasons[0]
    impact = store.latest_artifact("impact")
    assert impact is not None
    assert impact.content["migrations_needed"] is True
    assert "tests.service.test_api_links" in impact.content["tests_to_update"]

    # ---------------------------------------------------------------- ambiguous (human gates)
    amb_dir, amb_state, _ = cli.run_scenario(
        "ambiguous",
        workspace=ws,
        auto_approve="low,medium",
        high_risk=False,
        interactive=False,
        provider="deterministic",
    )
    assert amb_state is RunState.PAUSED
    gates_hit = []
    for _ in range(4):
        orch = runner.open_run(amb_dir)
        pending = orch.store.approvals(ApprovalStatus.PENDING)
        if not pending:
            break
        assert all(p.risk.value == "high" for p in pending)
        for p in pending:
            gates_hit.append(p.gate_id)
            orch.approve(p.gate_id, approved=True, comment="reviewed")
        amb_state = asyncio.run(runner.open_run(amb_dir).resume())
    assert amb_state is RunState.SUCCEEDED
    assert gates_hit == ["replan-1", "implement.api_key_auth#1", "release.readiness#1"]
    amb = metrics(amb_dir)
    assert amb["replans"] == 1
    assert amb["approvals"] >= 5
    store = StateStore(amb_dir / "state.db")
    spec = store.latest_artifact("spec")
    assert spec is not None
    assert spec.version == 2
    assert "api_key_auth" in spec.content["capabilities"]
    decided = {a.gate_id: a for a in store.approvals()}
    assert decided["replan-1"].approver == "test-reviewer@example.com"
    assert decided["intake#1"].auto  # clarification gate (medium) auto-approved with assumptions
    kinds = events(amb_dir)
    for expected in ("scripted_event", "change_detected", "replanned", "approval_blocked"):
        assert expected in kinds
    perf = store.latest_artifact("performance")
    assert perf is not None
    assert perf.content["p95_ms"] <= perf.content["target_p95_ms"]
    for run_dir in (gf_dir, bf_dir, amb_dir):
        assert verify_chain(run_dir / "audit.jsonl").ok

    # The repository's committed service is exactly what the orchestrator produced.
    assert cli.lineage_diff(ws, runner.REPO_ROOT) == []
