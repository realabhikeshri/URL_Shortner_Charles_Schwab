"""Engine + real git workspace: rollback, policy blocks, approval integrity, drift re-planning."""

import sys
from pathlib import Path

from orchestrator.approvals import AutonomyPolicy
from orchestrator.engine import RunConfig
from orchestrator.models import (
    ApprovalStatus,
    DeltaPlan,
    NodeState,
    RequirementSpec,
    RiskLevel,
    RunState,
    TaskNode,
)
from orchestrator.policy.engine import PolicyEngine
from orchestrator.providers.base import LLMClient
from orchestrator.workspace import Workspace
from tests.orchestrator.fakes import FakeAgent, Step, make_orchestrator, node, reopen

ROOT = Path(__file__).resolve().parents[2]
FAST = RunConfig.model_validate({"retry": {"base_delay_s": 0, "max_delay_s": 0}})


def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    (root / "service").mkdir(parents=True)
    (root / "service" / "app.py").write_text("VERSION = 1\n")
    return Workspace(root, python=sys.executable)


def writer(node_id: str, deps: list[str] | None = None, **kw: object) -> TaskNode:
    return node(node_id, deps, write_paths=["service/**"], **kw)  # type: ignore[arg-type]


async def test_failed_write_attempts_are_rolled_back_and_downstream_blocked(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    agent = FakeAgent(
        {
            "impl": [
                Step(
                    ok=False,
                    writes={"service/app.py": "VERSION = 2\n", "service/new.py": "X = 1\n"},
                )
            ]
        }
    )
    nodes = [writer("impl", max_attempts=2), node("docs", ["impl"])]
    orch = make_orchestrator(tmp_path, nodes, agent, config=FAST, workspace=ws)
    assert await orch.start() is RunState.FAILED
    # the workspace is exactly as it was at the checkpoint
    assert ws.read("service/app.py") == "VERSION = 1\n"
    assert not ws.exists("service/new.py")
    assert orch.store.node_states() == {"impl": NodeState.FAILED, "docs": NodeState.BLOCKED}
    impl_events = [e["to_state"] for e in orch.store.node_events() if e["node_id"] == "impl"]
    assert impl_events == [
        "READY", "RUNNING", "ROLLED_BACK", "RETRYING", "RUNNING", "ROLLED_BACK", "FAILED",
    ]  # fmt: skip
    rollbacks = [e for e in orch.audit.events() if e["type"] == "rollback"]
    assert len(rollbacks) == 2
    assert (orch.run_dir / "diffs" / "impl.attempt1.diff").exists()


async def test_successful_write_is_committed_with_decision_lineage(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    agent = FakeAgent({"impl": [Step(writes={"service/app.py": "VERSION = 2\n"})]})
    orch = make_orchestrator(tmp_path, [writer("impl")], agent, config=FAST, workspace=ws)
    assert await orch.start() is RunState.SUCCEEDED
    sha = await ws.blame_commit("service/app.py", 1)
    assert sha is not None
    message = await ws.commit_message(sha)
    decision = orch.decisions.latest_for_nodes(["impl"])[0]
    assert f"decision: {decision}" in message
    changes = orch.context.content("changes:impl")
    assert changes["files"] == ["service/app.py"]


async def test_agent_cannot_write_outside_its_declared_paths(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    agent = FakeAgent({"impl": [Step(writes={"docs/x.md": "hi"})]})
    n = node("impl", write_paths=["service/**"], max_attempts=1)
    orch = make_orchestrator(tmp_path, [n], agent, config=FAST, workspace=ws)
    assert await orch.start() is RunState.FAILED
    assert "write_paths" in (orch.store.node_row("impl")["error"] or "")
    assert not ws.exists("docs/x.md")


async def test_policy_block_rolls_back_and_feeds_violation_to_retry(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    leak = 'TOKEN = "sk-ant-abcdefghijklmnopqrstuvwxyz0123"\n'
    agent = FakeAgent(
        {
            "impl": [
                Step(writes={"service/cfg.py": leak}),
                Step(writes={"service/cfg.py": "X = 1\n"}),
            ]
        }
    )
    orch = make_orchestrator(
        tmp_path,
        [writer("impl")],
        agent,
        config=FAST,
        workspace=ws,
        policy_engine=PolicyEngine.from_dir(ROOT / "policies"),
    )
    assert await orch.start() is RunState.SUCCEEDED
    assert "SEC-001-secrets" in agent.calls[1].feedback[0]
    assert ws.read("service/cfg.py") == "X = 1\n"
    blocked = [p for p in orch.store.policy_results() if p["outcome"] == "block"]
    assert [p["policy_id"] for p in blocked] == ["SEC-001-secrets"]


async def test_high_risk_change_pauses_and_tampering_after_request_is_detected(
    tmp_path: Path,
) -> None:
    ws = workspace(tmp_path)
    agent = FakeAgent(
        {"auth": [Step(writes={"service/auth.py": "def verify() -> bool:\n    return True\n"})]}
    )
    nodes = [writer("auth"), node("after", ["auth"])]
    orch = make_orchestrator(
        tmp_path,
        nodes,
        agent,
        config=RunConfig.model_validate({**FAST.model_dump(), "auto_approve": "low,medium"}),
        workspace=ws,
        autonomy=AutonomyPolicy.load(ROOT / "policies" / "autonomy.yaml"),
    )
    assert await orch.start() is RunState.PAUSED
    pending = orch.store.approvals(ApprovalStatus.PENDING)
    assert pending[0].risk is RiskLevel.HIGH
    assert "security_sensitive" in pending[0].reasons[0]

    # someone edits the file after the approval was requested -> approval no longer valid
    (ws.root / "service" / "auth.py").write_text(
        "def verify() -> bool:\n    return False  # sneaky\n"
    )
    agent2 = FakeAgent(
        {"auth": [Step(writes={"service/auth.py": "def verify() -> bool:\n    return 1 == 1\n"})]}
    )
    resumed = reopen(
        tmp_path,
        agent2,
        workspace=ws,
        autonomy=AutonomyPolicy.load(ROOT / "policies" / "autonomy.yaml"),
    )
    resumed.approve(pending[0].gate_id, approved=True, comment="ok", approver="dana")
    assert await resumed.resume() is RunState.PAUSED  # re-ran and asked again for the new diff
    kinds = [e["type"] for e in resumed.audit.events()]
    assert "approval_invalidated" in kinds
    assert "sneaky" not in ws.read("service/auth.py")
    assert [a.gate_id for a in resumed.store.approvals(ApprovalStatus.PENDING)] == ["auth#2"]


class FakeReplanner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def reanalyze(
        self, requirement_text: str, previous: RequirementSpec | None, llm: LLMClient | None
    ) -> RequirementSpec:
        return RequirementSpec(
            id="R", title="t", goal=requirement_text[:20], functional=["x"], risk=RiskLevel.HIGH
        )

    async def delta(self, *, old_spec, new_spec, nodes, states, changed_nodes, llm):  # type: ignore[no-untyped-def]
        self.calls.append(changed_nodes)
        if changed_nodes:
            return DeltaPlan(rationale="re-validate after human edit")
        return DeltaPlan(
            add=[
                node("security", ["impl"], risk="high"),
            ],
            modify=[],
            rationale="requirement now asks for security hardening",
        )


async def test_human_edit_mid_run_invalidates_downstream(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    agent = FakeAgent({"impl": [Step(writes={"service/app.py": "VERSION = 2\n"})]})
    events = tmp_path / "events.yaml"
    events.write_text(
        "- after_node: verify\n  action: edit_file\n  path: service/app.py\n"
        '  append: "HOTFIX = True\\n"\n  note: human hot-fix\n'
    )
    cfg = RunConfig.model_validate({**FAST.model_dump(), "events_path": str(events)})
    replanner = FakeReplanner()
    nodes = [writer("impl"), node("verify", ["impl"])]
    orch = make_orchestrator(tmp_path, nodes, agent, config=cfg, workspace=ws, replanner=replanner)
    assert await orch.start() is RunState.SUCCEEDED
    assert replanner.calls == [["impl"]]
    verify_runs = [c for c in agent.calls if c.node_id == "verify"]
    assert len(verify_runs) == 2  # re-validated after the human edit
    assert len([c for c in agent.calls if c.node_id == "impl"]) == 1  # producer not redone
    replanned = [e for e in orch.audit.events() if e["type"] == "replanned"]
    assert replanned[0]["payload"]["invalidated"] == ["verify"]


async def test_requirement_change_replans_and_risk_increase_needs_approval(tmp_path: Path) -> None:
    events = tmp_path / "events.yaml"
    events.write_text(
        "- after_node: impl\n  action: amend_requirement\n  append: Also require auth.\n  note: stakeholder\n"
    )
    cfg = RunConfig.model_validate(
        {**FAST.model_dump(), "events_path": str(events), "auto_approve": "low,medium"}
    )
    agent = FakeAgent()
    replanner = FakeReplanner()
    orch = make_orchestrator(tmp_path, [node("impl")], agent, config=cfg, replanner=replanner)
    assert await orch.start() is RunState.PAUSED
    assert orch.store.approvals(ApprovalStatus.PENDING)[0].gate_id == "replan-1"
    assert "security" in orch.store.node_states()
    assert orch.store.node_states()["security"] is NodeState.PENDING  # held until approved

    resumed = reopen(tmp_path, FakeAgent(), replanner=replanner)
    resumed.approve("replan-1", approved=True, comment="security sign-off", approver="erin")
    assert await resumed.resume() is RunState.SUCCEEDED
    assert resumed.store.node_states()["security"] is NodeState.SUCCEEDED
    spec = resumed.context.typed("spec", RequirementSpec)
    assert spec is not None and spec.risk is RiskLevel.HIGH
    assert (resumed.run_dir / "plan.v1.mmd").exists()
