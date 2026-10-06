"""Fake agents and helpers for engine tests (no LLM, optional workspace)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.agents.base import READ, RUN, WRITE, Agent, AgentContext
from orchestrator.engine import EngineDeps, Orchestrator, RunConfig
from orchestrator.gates import GateContext, GateRegistry
from orchestrator.gates import registry as builtin_gates
from orchestrator.models import AgentResult, ChangeSet, FileOp, GateResult, TaskNode, TaskType


@dataclass
class Call:
    node_id: str
    attempt: int
    feedback: list[str]
    start: float
    end: float = 0.0


@dataclass
class Step:
    """One scripted attempt. ``ok=False`` makes the custom ``flag_ok`` exit gate fail."""

    ok: bool = True
    sleep: float = 0.0
    raise_exc: bool = False
    artifacts: dict[str, Any] = field(default_factory=dict)
    writes: dict[str, str] = field(default_factory=dict)
    approval: dict[str, Any] | None = None


class FakeAgent(Agent):
    name = "Fake"
    permissions = frozenset({READ, WRITE, RUN})

    def __init__(self, script: dict[str, list[Step]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[Call] = []
        self.active = 0
        self.max_active = 0

    async def run(self, ctx: AgentContext) -> AgentResult:
        steps = self.script.get(ctx.node.id, [Step()])
        step = steps[min(ctx.attempt - 1, len(steps) - 1)]
        call = Call(ctx.node.id, ctx.attempt, list(ctx.feedback), time.monotonic())
        self.calls.append(call)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if step.sleep:
                await asyncio.sleep(step.sleep)
            if step.raise_exc:
                raise RuntimeError(f"scripted crash in {ctx.node.id}")
        finally:
            self.active -= 1
            call.end = time.monotonic()
        artifacts = {k: {"from": ctx.node.id} for k in ctx.node.outputs}
        artifacts.update(step.artifacts)
        artifacts["ok"] = step.ok
        if step.approval:
            artifacts["approval_request"] = step.approval
        changeset = None
        if step.writes:
            changeset = ChangeSet(
                summary="scripted writes",
                ops=[FileOp(op="write", path=p, content=c) for p, c in step.writes.items()],
            )
        return AgentResult(
            summary=f"{ctx.node.id} attempt {ctx.attempt}",
            artifacts=artifacts,
            changeset=changeset,
            rationale=f"scripted behaviour for {ctx.node.id}",
            alternatives=["do nothing"],
        )


def gate_registry() -> GateRegistry:
    reg = GateRegistry()
    for name in builtin_gates.names():
        reg.add(name, builtin_gates._gates[name])

    async def flag_ok(ctx: GateContext, _: str | None) -> GateResult:
        ok = bool(ctx.result and ctx.result.artifacts.get("ok"))
        return GateResult(gate="flag_ok", passed=ok, details="ok" if ok else "AssertionError: x")

    reg.add("flag_ok", flag_ok)
    return reg


def node(
    node_id: str,
    deps: list[str] | None = None,
    *,
    type_: TaskType = TaskType.IMPLEMENT,
    outputs: list[str] | None = None,
    inputs: list[str] | None = None,
    max_attempts: int = 3,
    write_paths: list[str] | None = None,
    optional: bool = False,
    **kw: Any,
) -> TaskNode:
    return TaskNode(
        id=node_id,
        type=type_,
        title=node_id,
        agent="Fake",
        depends_on=deps or [],
        outputs=outputs or [],
        inputs=inputs or [],
        exit_criteria=["flag_ok"],
        max_attempts=max_attempts,
        write_paths=write_paths or [],
        optional=optional,
        **kw,
    )


def make_orchestrator(
    tmp: Path,
    nodes: list[TaskNode],
    agent: FakeAgent,
    *,
    config: RunConfig | None = None,
    run_id: str = "run-test",
    **deps: Any,
) -> Orchestrator:
    run_dir = tmp / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    req = tmp / "requirement.txt"
    if not req.exists():
        req.write_text("Build a thing.\n")
    cfg = config or RunConfig(retry={"base_delay_s": 0.0, "max_delay_s": 0.0})  # type: ignore[arg-type]
    orch = Orchestrator(
        run_dir, EngineDeps(agents={"Fake": agent}, gates=gate_registry(), **deps), config=cfg
    )
    orch.create(requirement_path=req, bootstrap_nodes=nodes)
    return orch


def reopen(tmp: Path, agent: FakeAgent, *, run_id: str = "run-test", **deps: Any) -> Orchestrator:
    run_dir = tmp / "runs" / run_id
    from orchestrator.state import StateStore

    cfg = RunConfig.model_validate(StateStore(run_dir / "state.db").run()["config"])
    return Orchestrator(
        run_dir, EngineDeps(agents={"Fake": agent}, gates=gate_registry(), **deps), config=cfg
    )
