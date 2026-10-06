"""Engine behaviour with fake agents: states, gates, parallelism, joins, retries, approvals."""

from pathlib import Path

import pytest

from orchestrator.audit import verify_chain
from orchestrator.engine import RunConfig
from orchestrator.models import (
    ApprovalStatus,
    NodeState,
    PlanDAG,
    RunState,
    TaskType,
)
from tests.orchestrator.fakes import FakeAgent, Step, make_orchestrator, node, reopen

FAST = {"retry": {"base_delay_s": 0.0, "max_delay_s": 0.0}}


def cfg(**kw: object) -> RunConfig:
    return RunConfig.model_validate({**FAST, **kw})


async def test_linear_run_succeeds_and_records_everything(tmp_path: Path) -> None:
    agent = FakeAgent()
    nodes = [node("a", outputs=["x"]), node("b", ["a"], inputs=["x"])]
    orch = make_orchestrator(tmp_path, nodes, agent)
    assert await orch.start() is RunState.SUCCEEDED
    assert orch.store.node_states() == {"a": NodeState.SUCCEEDED, "b": NodeState.SUCCEEDED}
    assert [c.node_id for c in agent.calls] == ["a", "b"]
    assert orch.context.get("x") is not None
    assert verify_chain(orch.run_dir / "audit.jsonl").ok
    # decision lineage: b's decision descends from a's, which descends from the requirement
    b_decision = orch.decisions.latest_for_nodes(["b"])[0]
    chain = [d.node_id for d in orch.decisions.lineage(b_decision)]
    assert chain == ["b", "a", "requirement"]
    # gate results persisted
    assert {g["gate"] for g in orch.store.gate_results()} == {"inputs_available", "flag_ok"}
    events = [e["to_state"] for e in orch.store.node_events() if e["node_id"] == "a"]
    assert events == ["READY", "RUNNING", "SUCCEEDED"]


async def test_parallel_branches_run_concurrently_and_join_waits(tmp_path: Path) -> None:
    script = {"tests": [Step(sleep=0.3)], "docs": [Step(sleep=0.3)]}
    agent = FakeAgent(script)
    nodes = [
        node("impl"),
        node("tests", ["impl"]),
        node("docs", ["impl"]),
        node("release", ["tests", "docs"], type_=TaskType.RELEASE_CHECK),
    ]
    orch = make_orchestrator(tmp_path, nodes, agent)
    assert await orch.start() is RunState.SUCCEEDED
    calls = {c.node_id: c for c in agent.calls}
    assert agent.max_active == 2
    # overlap: docs started before tests finished and vice versa
    assert calls["docs"].start < calls["tests"].end
    assert calls["tests"].start < calls["docs"].end
    # join: release starts only after both branches end
    assert calls["release"].start >= max(calls["tests"].end, calls["docs"].end)


async def test_concurrency_limit_is_respected(tmp_path: Path) -> None:
    script = {f"n{i}": [Step(sleep=0.15)] for i in range(5)}
    agent = FakeAgent(script)
    nodes = [node("root")] + [node(f"n{i}", ["root"]) for i in range(5)]
    orch = make_orchestrator(tmp_path, nodes, agent, config=cfg(concurrency=2))
    assert await orch.start() is RunState.SUCCEEDED
    assert agent.max_active == 2


async def test_retry_feeds_gate_failure_back_and_recovers(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(ok=False), Step(ok=True)]})
    orch = make_orchestrator(tmp_path, [node("a")], agent)
    assert await orch.start() is RunState.SUCCEEDED
    assert [c.attempt for c in agent.calls] == [1, 2]
    assert agent.calls[0].feedback == []
    assert "AssertionError" in agent.calls[1].feedback[0]
    states = [e["to_state"] for e in orch.store.node_events()]
    assert states == ["READY", "RUNNING", "RETRYING", "RUNNING", "SUCCEEDED"]


async def test_exhausted_retries_fail_node_and_block_downstream(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(ok=False)]})
    nodes = [node("a", max_attempts=2), node("b", ["a"]), node("c", ["b"])]
    orch = make_orchestrator(tmp_path, nodes, agent)
    assert await orch.start() is RunState.FAILED
    assert orch.store.node_states() == {
        "a": NodeState.FAILED,
        "b": NodeState.BLOCKED,
        "c": NodeState.BLOCKED,
    }
    assert len(agent.calls) == 2
    assert "AssertionError" in (orch.store.node_row("a")["error"] or "")


async def test_agent_exception_is_contained_and_retried(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(raise_exc=True), Step()]})
    orch = make_orchestrator(tmp_path, [node("a")], agent)
    assert await orch.start() is RunState.SUCCEEDED
    assert "scripted crash" in agent.calls[1].feedback[0]


async def test_optional_node_failure_does_not_block(tmp_path: Path) -> None:
    agent = FakeAgent({"opt": [Step(ok=False)]})
    nodes = [node("opt", max_attempts=1, optional=True), node("b", ["opt"])]
    orch = make_orchestrator(tmp_path, nodes, agent)
    assert await orch.start() is RunState.SUCCEEDED
    assert orch.store.node_states()["b"] is NodeState.SUCCEEDED


async def test_entry_gate_failure_fails_without_running_agent(tmp_path: Path) -> None:
    agent = FakeAgent()
    bad = node("a", entry_criteria=["nonexistent_gate"])
    orch = make_orchestrator(tmp_path, [bad], agent)
    assert await orch.start() is RunState.FAILED
    assert agent.calls == []


async def test_retry_budget_caps_total_retries(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(ok=False)], "b": [Step(ok=False)]})
    nodes = [node("a", max_attempts=5), node("b", max_attempts=5)]
    nodes = [nodes[0], nodes[1].model_copy(update={"depends_on": ["a"], "optional": False})]
    nodes[0] = nodes[0].model_copy(update={"optional": True})
    config = cfg(budgets={"max_retries_per_run": 2})
    orch = make_orchestrator(tmp_path, nodes, agent, config=config)
    await orch.start()
    # a: 1 attempt + 2 retries, then the per-run retry budget is exhausted
    assert [c.node_id for c in agent.calls].count("a") == 3


async def test_plan_artifact_expands_the_dag(tmp_path: Path) -> None:
    planned = PlanDAG(
        nodes=[
            node("impl", outputs=["code"]),
            node("verify", ["impl"], inputs=["code"], type_=TaskType.TEST),
        ]
    )
    agent = FakeAgent({"plan": [Step(artifacts={"plan": planned.model_dump(mode="json")})]})
    orch = make_orchestrator(tmp_path, [node("plan", type_=TaskType.PLAN, outputs=["plan"])], agent)
    assert await orch.start() is RunState.SUCCEEDED
    assert [n.id for n in orch.store.nodes()] == ["plan", "impl", "verify"]
    assert orch.store.nodes()[1].depends_on == ["plan"]
    assert (orch.run_dir / "plan.mmd").exists()


async def test_invalid_plan_is_rejected_and_retried(tmp_path: Path) -> None:
    cyclic = PlanDAG(nodes=[node("x", ["y"]), node("y", ["x"])])
    good = PlanDAG(nodes=[node("x")])
    agent = FakeAgent(
        {
            "plan": [
                Step(artifacts={"plan": cyclic.model_dump(mode="json")}),
                Step(artifacts={"plan": good.model_dump(mode="json")}),
            ]
        }
    )
    orch = make_orchestrator(tmp_path, [node("plan", type_=TaskType.PLAN, outputs=["plan"])], agent)
    assert await orch.start() is RunState.SUCCEEDED
    assert "cycle" in agent.calls[1].feedback[0]


async def test_kill_switch_safe_stops_and_resume_completes(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(sleep=0.3)]})
    nodes = [node("a"), node("b", ["a"])]
    orch = make_orchestrator(tmp_path, nodes, agent)
    orch.kill_switch.trigger("operator requested stop")
    assert await orch.start() is RunState.SAFE_STOPPED
    assert orch.store.run()["state"] == RunState.SAFE_STOPPED.value
    assert agent.calls == []

    agent2 = FakeAgent()
    resumed = reopen(tmp_path, agent2)
    assert await resumed.resume() is RunState.SUCCEEDED
    assert [c.node_id for c in agent2.calls] == ["a", "b"]


async def test_wall_time_budget_breach_safe_stops(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(sleep=0.4)]})
    nodes = [node("a"), node("b", ["a"])]
    orch = make_orchestrator(tmp_path, nodes, agent, config=cfg(budgets={"max_wall_time_s": 0.2}))
    assert await orch.start() is RunState.SAFE_STOPPED
    # the in-flight node finished gracefully; nothing new was scheduled
    assert orch.store.node_states() == {"a": NodeState.SUCCEEDED, "b": NodeState.PENDING}
    kinds = [e["type"] for e in orch.audit.events()]
    assert "budget_breach" in kinds
    assert "safe_stop" in kinds


APPROVAL = {"reasons": ["needs a human"], "risk": "medium"}
HIGH = {"reasons": ["auth change"], "risk": "high"}


async def test_approval_pauses_then_cli_approve_and_resume(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(approval=APPROVAL)]})
    nodes = [node("a"), node("b", ["a"])]
    orch = make_orchestrator(tmp_path, nodes, agent)
    assert await orch.start() is RunState.PAUSED
    assert orch.store.node_states()["a"] is NodeState.AWAITING_APPROVAL
    pending = orch.store.approvals(ApprovalStatus.PENDING)
    assert [p.gate_id for p in pending] == ["a#1"]

    agent2 = FakeAgent()
    resumed = reopen(tmp_path, agent2)
    decided = resumed.approve("a#1", approved=True, comment="looks good", approver="alice")
    assert decided.approver == "alice"
    assert await resumed.resume() is RunState.SUCCEEDED
    assert [c.node_id for c in agent2.calls] == ["b"]  # a was not re-executed
    assert orch.store.approval("a#1").status is ApprovalStatus.APPROVED  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already approved"):
        resumed.approve("a#1", approved=True, comment="again")
    with pytest.raises(KeyError):
        resumed.approve("nope", approved=True, comment="")


async def test_rejection_retries_with_reviewer_feedback(tmp_path: Path) -> None:
    agent = FakeAgent({"a": [Step(approval=APPROVAL)]})
    orch = make_orchestrator(tmp_path, [node("a")], agent)
    assert await orch.start() is RunState.PAUSED
    agent2 = FakeAgent({"a": [Step()]})
    resumed = reopen(tmp_path, agent2)
    resumed.approve("a#1", approved=False, comment="use a constant-time compare", approver="bob")
    assert await resumed.resume() is RunState.SUCCEEDED
    assert agent2.calls[0].attempt == 2
    assert "constant-time" in agent2.calls[0].feedback[-1]


async def test_auto_approve_levels_and_high_risk_flag(tmp_path: Path) -> None:
    # medium is auto-approved, high is not
    agent = FakeAgent({"m": [Step(approval=APPROVAL)], "h": [Step(approval=HIGH)]})
    nodes = [node("m"), node("h", ["m"])]
    orch = make_orchestrator(
        tmp_path, nodes, agent, config=cfg(auto_approve="low,medium"), run_id="r1"
    )
    assert await orch.start() is RunState.PAUSED
    states = orch.store.node_states()
    assert states == {"m": NodeState.SUCCEEDED, "h": NodeState.AWAITING_APPROVAL}
    m = orch.store.approval("m#1")
    assert m is not None and m.auto and m.status is ApprovalStatus.APPROVED

    # with the explicit flag the high-risk gate passes and the override is audited
    agent2 = FakeAgent({"m": [Step(approval=APPROVAL)], "h": [Step(approval=HIGH)]})
    orch2 = make_orchestrator(
        tmp_path,
        nodes,
        agent2,
        config=cfg(auto_approve="low,medium", allow_high_risk=True),
        run_id="r2",
    )
    assert await orch2.start() is RunState.SUCCEEDED
    assert "high_risk_override" in [e["type"] for e in orch2.audit.events()]


async def test_interactive_prompter_decides(tmp_path: Path) -> None:
    asked = []

    async def prompter(req):  # type: ignore[no-untyped-def]
        asked.append(req.gate_id)
        return True, "ship it"

    agent = FakeAgent({"a": [Step(approval=APPROVAL)]})
    orch = make_orchestrator(
        tmp_path, [node("a")], agent, config=cfg(interactive=True), prompter=prompter
    )
    assert await orch.start() is RunState.SUCCEEDED
    assert asked == ["a#1"]
    approval = orch.store.approval("a#1")
    assert approval is not None and approval.comment == "ship it" and not approval.auto
