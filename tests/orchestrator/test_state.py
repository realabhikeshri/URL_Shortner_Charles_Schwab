from pathlib import Path

import pytest

from orchestrator.context import ContextStore, DecisionLog
from orchestrator.controls import Budgets, BudgetTracker, KillSwitch, RetryPolicy
from orchestrator.models import NodeState, RiskLevel, RunState
from orchestrator.state import StateStore
from orchestrator.tracing import Tracer
from tests.orchestrator.fakes import node


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.db")
    store.create_run("r", scenario="s", requirement_path="req", workspace="ws", config={"a": 1})
    return store


def test_run_and_node_persistence_survives_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_node(node("a"), plan_version=1)
    store.set_node_state("a", NodeState.READY, attempt=0)
    store.set_run_state(RunState.SUCCEEDED)
    store.set_run_fields(risk="high")
    store.close()

    again = StateStore(tmp_path / "state.db")
    run = again.run()
    assert run["config"] == {"a": 1}
    assert run["state"] == "SUCCEEDED" and run["finished_at"]
    assert run["risk"] == "high"
    assert again.node_states() == {"a": NodeState.READY}
    with pytest.raises(ValueError, match="unknown run fields"):
        again.set_run_fields(bogus=1)
    with pytest.raises(ValueError, match="unknown node fields"):
        again.update_node("a", bogus=1)
    with pytest.raises(KeyError):
        again.node_row("zzz")


def test_artifacts_are_versioned_and_hashed(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("x = 1\n")
    ctx = ContextStore(_store(tmp_path), ws)
    a1 = ctx.publish("spec", {"v": 1}, node_id="n", paths=["f.py"])
    a2 = ctx.publish("spec", {"v": 2}, node_id="n")
    assert (a1.version, a2.version) == (1, 2)
    assert a1.content_hash != a2.content_hash
    assert a1.paths["f.py"] is not None
    assert ctx.content("spec") == {"v": 2}
    assert ctx.content("missing", "dflt") == "dflt"
    assert not ctx.has("missing")
    assert len(ctx.store.artifacts(latest_only=False)) == 2


def test_decision_lineage_walks_to_root(tmp_path: Path) -> None:
    log = DecisionLog(_store(tmp_path))
    root = log.record(node_id="requirement", agent="human", inputs="r", rationale="root")
    mid = log.record(
        node_id="plan", agent="Planner", inputs={}, rationale="p", parents=[root.decision_id]
    )
    leaf = log.record(
        node_id="impl",
        agent="Implementer",
        inputs={},
        rationale="i",
        parents=[mid.decision_id],
        alternatives=["other"],
    )
    assert [d.node_id for d in log.lineage(leaf.decision_id)] == ["impl", "plan", "requirement"]
    assert log.latest_for_nodes(["plan", "nope"]) == [mid.decision_id]


def test_tracer_nests_spans_and_records_errors(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tracer = Tracer("r", store)
    with tracer.span("outer", node_id="n") as outer, tracer.span("inner") as inner:
        inner.set(k="v")
    with pytest.raises(RuntimeError), tracer.span("boom"):
        raise RuntimeError("x")
    spans = {s["name"]: s for s in store.spans()}
    assert spans["inner"]["parent_id"] == outer.span_id
    assert spans["inner"]["node_id"] == "n"
    assert spans["inner"]["attributes"] == {"k": "v"}
    assert spans["boom"]["status"] == "ERROR"
    assert tracer.export_jsonl(tmp_path / "t.jsonl") == 3


def test_retry_policy_backoff_and_jitter() -> None:
    policy = RetryPolicy(base_delay_s=1, max_delay_s=5, jitter=False)
    assert [policy.delay(a) for a in (1, 2, 3, 4)] == [1, 2, 4, 5]
    jittered = RetryPolicy(base_delay_s=1, max_delay_s=5)
    assert all(0 <= jittered.delay(3) <= 4 for _ in range(50))


def test_budget_tracker_reports_first_breach() -> None:
    tracker = BudgetTracker(Budgets(max_llm_tokens=100, max_files_changed=1))
    assert tracker.check() is None
    tracker.add_tokens(150, 0.1)
    breach = tracker.check()
    assert breach is not None and breach.budget == "max_llm_tokens"
    t2 = BudgetTracker(Budgets(max_files_changed=1))
    t2.add_files(["a", "b"])
    assert t2.check().budget == "max_files_changed"  # type: ignore[union-attr]
    assert BudgetTracker(Budgets(max_llm_tokens=10)).remaining_tokens() == 10


def test_kill_switch(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path)
    assert ks.triggered() is None
    ks.trigger("enough")
    assert ks.triggered() == "enough"
    ks.clear()
    assert ks.triggered() is None


def test_risk_level_ordering() -> None:
    assert RiskLevel.max(RiskLevel.LOW, RiskLevel.HIGH, RiskLevel.MEDIUM) is RiskLevel.HIGH
    assert RiskLevel.max() is RiskLevel.LOW
