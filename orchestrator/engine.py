"""The execution engine.

Runs a plan DAG of agent tasks with:

* a strict per-node state machine persisted to SQLite before every action (resumable);
* entry/exit gates, policy evaluation and human approval checkpoints;
* bounded retries with exponential backoff + jitter, feeding gate output back to the agent;
* path-scoped git rollback of failed write attempts, blocking of downstream nodes;
* asyncio parallelism with a concurrency limit; join nodes wait for all predecessors;
* safe-stop (kill switch + hard budgets) and dynamic re-planning on upstream changes;
* a hash-chained audit log, decision lineage and OTel-style spans for everything above.
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, Field

from orchestrator import dag
from orchestrator.agents.base import WRITE, Agent, AgentContext
from orchestrator.approvals import (
    ApprovalNeed,
    AutoApprovePolicy,
    AutonomyPolicy,
    approver_identity,
    decide,
    evaluate_autonomy,
)
from orchestrator.audit import AuditLog
from orchestrator.context import ContextStore, DecisionLog
from orchestrator.controls import BudgetBreach, Budgets, BudgetTracker, KillSwitch, RetryPolicy
from orchestrator.gates import GateContext, GateRegistry
from orchestrator.gates import registry as default_gates
from orchestrator.hashing import sha256_file, sha256_json
from orchestrator.models import (
    DONE_OK_STATES,
    NODE_TRANSITIONS,
    TERMINAL_NODE_STATES,
    AgentResult,
    ApprovalRequest,
    ApprovalStatus,
    DeltaPlan,
    LLMUsage,
    NodeState,
    PlanDAG,
    PolicyOutcome,
    RequirementSpec,
    RiskLevel,
    RunState,
    TaskNode,
)
from orchestrator.policy.engine import PolicyEngine, PolicyInput
from orchestrator.providers.base import LLMClient
from orchestrator.state import StateStore
from orchestrator.tracing import Tracer
from orchestrator.workspace import (
    AppliedChange,
    ApplyError,
    ToolBelt,
    Workspace,
    WorkspaceViolation,
)


class RunConfig(BaseModel):
    concurrency: int = Field(4, ge=1, le=32)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    budgets: Budgets = Field(default_factory=Budgets)
    auto_approve: str | None = None
    allow_high_risk: bool = False
    interactive: bool = False
    ambiguity_threshold: float = 0.35
    provider: str = "deterministic"
    model: str | None = None
    events_path: str | None = None
    scenario: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


Prompter = Callable[[ApprovalRequest], Awaitable[tuple[bool, str]]]
Finalizer = Callable[["Orchestrator"], None]


class Replanner(Protocol):
    async def reanalyze(
        self, requirement_text: str, previous: RequirementSpec | None, llm: LLMClient | None
    ) -> RequirementSpec: ...

    async def delta(
        self,
        *,
        old_spec: RequirementSpec | None,
        new_spec: RequirementSpec | None,
        nodes: list[TaskNode],
        states: dict[str, NodeState],
        changed_nodes: list[str],
        llm: LLMClient | None,
    ) -> DeltaPlan: ...


@dataclass
class AttemptOutcome:
    status: Literal["succeeded", "failed", "awaiting"]
    feedback: str = ""
    applied: AppliedChange | None = None
    retryable: bool = True


@dataclass
class _Pending:
    result: AgentResult
    applied: dict[str, Any] | None
    decision_id: str
    gate_id: str
    approval_kind: str = "change"


@dataclass
class EngineDeps:
    agents: dict[str, Agent]
    workspace: Workspace | None = None
    policy_engine: PolicyEngine | None = None
    autonomy: AutonomyPolicy | None = None
    llm: LLMClient | None = None
    gates: GateRegistry = field(default_factory=lambda: default_gates)
    replanner: Replanner | None = None
    prompter: Prompter | None = None
    finalizers: list[Finalizer] = field(default_factory=list)


class Orchestrator:
    def __init__(self, run_dir: Path, deps: EngineDeps, *, config: RunConfig) -> None:
        self.run_dir = run_dir
        self.run_id = run_dir.name
        self.deps = deps
        self.config = config
        self.store = StateStore(run_dir / "state.db")
        self.audit = AuditLog(run_dir / "audit.jsonl", self.run_id)
        self.tracer = Tracer(self.run_id, self.store)
        self.ws = deps.workspace
        self.context = ContextStore(self.store, self.ws.root if self.ws else None)
        self.decisions = DecisionLog(self.store)
        self.kill_switch = KillSwitch(run_dir)
        self.budget = BudgetTracker(config.budgets)
        self.auto = AutoApprovePolicy.parse(
            config.auto_approve, allow_high_risk=config.allow_high_risk
        )
        self._rng = random.Random(sha256_json(self.run_id))  # noqa: S311 - jitter only
        self._stop_reason: str | None = None
        self._pending_replan_gate: str | None = None

    # ================================================================== lifecycle

    def create(
        self,
        *,
        requirement_path: Path,
        bootstrap_nodes: list[TaskNode],
        scenario: str | None = None,
    ) -> None:
        """Persist a new run: copy the requirement of record, seed the bootstrap DAG."""
        dag.validate(bootstrap_nodes)
        req_copy = self.run_dir / f"requirement{requirement_path.suffix or '.txt'}"
        shutil.copyfile(requirement_path, req_copy)
        self.store.create_run(
            self.run_id,
            scenario=scenario,
            requirement_path=str(req_copy),
            workspace=str(self.ws.root) if self.ws else "",
            config=self.config.model_dump(mode="json"),
        )
        for node in bootstrap_nodes:
            self.store.upsert_node(node, plan_version=0)
        text = req_copy.read_text(encoding="utf-8")
        self.context.publish("requirement", {"text": text, "path": str(req_copy)}, node_id="human")
        self.store.kv_set("requirement_hash", sha256_file(req_copy))
        root = self.decisions.record(
            node_id="requirement",
            agent="human",
            inputs={"requirement": text},
            rationale=f"Requirement of record: {requirement_path.name}",
            outcome="accepted for processing",
        )
        self.store.kv_set("root_decision", root.decision_id)
        self.audit.append(
            "run_created",
            scenario=scenario,
            requirement=str(requirement_path),
            requirement_sha256=sha256_file(req_copy),
            workspace=str(self.ws.root) if self.ws else None,
            config=self.config.model_dump(mode="json"),
            nodes=[n.id for n in bootstrap_nodes],
        )

    async def start(self) -> RunState:
        if self.ws is not None:
            await self.ws.init_git()
            if self.store.kv_get("baseline_commit") is None:
                self.store.kv_set("baseline_commit", await self.ws.head())
        self.audit.append("run_started", run_id=self.run_id)
        return await self._execute()

    async def resume(self) -> RunState:
        run = self.store.run()
        if run["state"] in {RunState.SUCCEEDED.value}:
            return RunState.SUCCEEDED
        self.kill_switch.clear()
        self.audit.append("run_resumed", previous_state=run["state"])
        if self.ws is not None:
            await self.ws.init_git()
        # Nodes that were RUNNING when the process died: roll back their partial writes and
        # put them back in the queue. Their attempt counter is preserved (bounded retries).
        for row in self.store.node_rows():
            state = NodeState(row["state"])
            node = TaskNode.model_validate_json(row["spec_json"])
            if state in {NodeState.RUNNING, NodeState.RETRYING, NodeState.ROLLED_BACK}:
                if row["checkpoint"] and self.ws is not None and node.is_write:
                    restored = await self._rollback_paths(node, row["checkpoint"])
                    self.audit.append("crash_recovery_rollback", node_id=node.id, paths=restored)
                self._transition(node.id, NodeState.READY, row["attempts"], "resumed after crash")
            elif state is NodeState.FAILED and run["state"] == RunState.SAFE_STOPPED.value:
                pass
        return await self._execute()

    # ================================================================== main loop

    async def _execute(self) -> RunState:
        self.store.set_run_state(RunState.RUNNING)
        running: dict[str, asyncio.Task[None]] = {}
        await self._process_decided_approvals()
        while True:
            if self._stop_reason is None:
                self._check_stop()
            if self._stop_reason is None and not running:
                await self._check_replan()
            if self._stop_reason is None and self._pending_replan_gate is None:
                self._promote_ready()
                for node in self._ready_nodes():
                    if len(running) >= self.config.concurrency:
                        break
                    running[node.id] = asyncio.create_task(self._run_node(node))
            if not running:
                break
            done, _ = await asyncio.wait(
                running.values(), timeout=0.2, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                node_id = next(k for k, v in running.items() if v is task)
                del running[node_id]
                exc = task.exception()
                if exc is not None:  # defensive: _run_node handles its own errors
                    self.audit.append("node_crashed", node_id=node_id, error=repr(exc))
                    self._fail_node(node_id, f"engine error: {exc!r}")
        final = self._final_state()
        self.store.set_run_state(final)
        self.audit.append(
            "run_finished" if final in {RunState.SUCCEEDED, RunState.FAILED} else "run_halted",
            state=final.value,
            reason=self._stop_reason,
            pending_approvals=[a.gate_id for a in self.store.approvals(ApprovalStatus.PENDING)],
        )
        for fin in self.deps.finalizers:
            fin(self)
        return final

    def _final_state(self) -> RunState:
        states = self.store.node_states()
        nodes = {n.id: n for n in self.store.nodes()}
        if self._stop_reason is not None:
            return RunState.SAFE_STOPPED
        pending = self.store.approvals(ApprovalStatus.PENDING)
        if pending or NodeState.AWAITING_APPROVAL in states.values():
            return RunState.PAUSED
        hard_fail = [
            n
            for n, s in states.items()
            if s in {NodeState.FAILED, NodeState.BLOCKED} and not nodes[n].optional
        ]
        if hard_fail:
            return RunState.FAILED
        if all(s in TERMINAL_NODE_STATES for s in states.values()):
            return RunState.SUCCEEDED
        return RunState.PAUSED

    def _check_stop(self) -> None:
        reason = self.kill_switch.triggered()
        if reason:
            self._stop_reason = f"kill switch: {reason}"
        else:
            breach: BudgetBreach | None = self.budget.check()
            if breach is not None:
                self._stop_reason = str(breach)
                self.audit.append(
                    "budget_breach", budget=breach.budget, used=breach.used, limit=breach.limit
                )
        if self._stop_reason:
            self.audit.append("safe_stop", reason=self._stop_reason)

    def _ready_nodes(self) -> list[TaskNode]:
        states = self.store.node_states()
        return [n for n in self.store.nodes() if states[n.id] is NodeState.READY]

    def _promote_ready(self) -> None:
        states = self.store.node_states()
        nodes = {n.id: n for n in self.store.nodes()}
        for node_id, state in states.items():
            if state is not NodeState.PENDING:
                continue
            deps = nodes[node_id].depends_on
            dep_states = [states.get(d, NodeState.SKIPPED) for d in deps]
            bad = [
                d
                for d, s in zip(deps, dep_states, strict=True)
                if s in {NodeState.FAILED, NodeState.BLOCKED} and not nodes[d].optional
            ]
            if bad:
                self._transition(node_id, NodeState.BLOCKED, 0, f"upstream failed: {bad}")
            elif all(
                s in DONE_OK_STATES or (s is NodeState.FAILED and nodes[d].optional)
                for d, s in zip(deps, dep_states, strict=True)
            ):
                self._transition(
                    node_id, NodeState.READY, self._attempts(node_id), "deps satisfied"
                )

    # ================================================================== state helpers

    def _attempts(self, node_id: str) -> int:
        return int(self.store.node_row(node_id)["attempts"])

    def _transition(self, node_id: str, to: NodeState, attempt: int, reason: str = "") -> None:
        current = self.store.node_states()[node_id]
        if current is to:
            return
        if to not in NODE_TRANSITIONS[current]:
            raise RuntimeError(f"illegal transition {node_id}: {current} -> {to}")
        self.store.set_node_state(node_id, to, attempt=attempt, reason=reason)
        self.audit.append(
            "node_state", node_id=node_id, frm=current.value, to=to.value, attempt=attempt,
            reason=reason[:500],
        )  # fmt: skip

    def _fail_node(self, node_id: str, reason: str) -> None:
        state = self.store.node_states()[node_id]
        attempt = self._attempts(node_id)
        if state is not NodeState.FAILED:
            if NodeState.FAILED not in NODE_TRANSITIONS[state]:
                self.store.set_node_state(node_id, NodeState.FAILED, attempt=attempt, reason=reason)
            else:
                self._transition(node_id, NodeState.FAILED, attempt, reason)
        self.store.update_node(node_id, error=reason[:4000])
        nodes = self.store.nodes()
        if next(n for n in nodes if n.id == node_id).optional:
            return  # optional work failing never blocks the rest of the plan
        for desc in sorted(dag.descendants(nodes, node_id)):
            if self.store.node_states()[desc] in {NodeState.PENDING, NodeState.READY}:
                self._transition(desc, NodeState.BLOCKED, 0, f"upstream {node_id} failed")

    def _llm_for(self, node_id: str) -> LLMClient | None:
        """Per-node LLM client so usage is attributed correctly under parallelism."""
        base = self.deps.llm
        if base is None:
            return None

        def sink(usage: LLMUsage) -> None:
            self._on_llm_usage(node_id, usage)

        return LLMClient(
            base.primary,
            fallback=base.fallback,
            on_usage=sink,
            token_budget=self.budget.remaining_tokens,
        )

    def _on_llm_usage(self, node_id: str, usage: LLMUsage) -> None:
        self.store.add_llm_call(node_id, usage)
        self.budget.add_tokens(usage.input_tokens + usage.output_tokens, usage.cost_usd)
        if usage.fallback:
            self.audit.append(
                "llm_fallback", node_id=node_id, role=usage.role, provider=usage.provider
            )

    # ================================================================== node execution

    async def _run_node(self, node: TaskNode) -> None:
        attempt = self._attempts(node.id)
        feedback: list[str] = list(self.store.kv_get(f"feedback:{node.id}", []))
        with self.tracer.span(
            f"node:{node.id}", node_id=node.id, type=node.type.value, agent=node.agent
        ) as span:
            entry = await self._gates(
                node, attempt, "entry", ["inputs_available", *node.entry_criteria]
            )
            if entry:
                self._transition(node.id, NodeState.RUNNING, attempt, "entry gate check")
                self._fail_node(node.id, f"entry gate failed: {entry}")
                span.status = "ERROR"
                return
            while attempt < node.max_attempts:
                attempt += 1
                if self._stop_reason is not None:
                    self._transition(
                        node.id, NodeState.READY, attempt - 1, "safe-stop before attempt"
                    )
                    return
                self._transition(node.id, NodeState.RUNNING, attempt, f"attempt {attempt}")
                self.store.increment_executions(node.id)
                checkpoint = None
                if self.ws is not None and node.is_write:
                    checkpoint = await self.ws.checkpoint(f"{node.id} attempt {attempt}")
                    self.store.update_node(node.id, checkpoint=checkpoint)
                with self.tracer.span(f"attempt:{node.id}#{attempt}", attempt=attempt):
                    outcome = await self._attempt(node, attempt, feedback)
                if outcome.status == "succeeded":
                    span.set(attempts=attempt)
                    return
                if outcome.status == "awaiting":
                    span.set(awaiting_approval=True)
                    return
                if outcome.applied is not None and checkpoint is not None:
                    restored = await self._rollback_paths(node, checkpoint, outcome.applied.files)
                    self._transition(
                        node.id, NodeState.ROLLED_BACK, attempt, outcome.feedback[:300]
                    )
                    self.audit.append("rollback", node_id=node.id, attempt=attempt, paths=restored)
                feedback.append(f"attempt {attempt} failed:\n{outcome.feedback}")
                self.store.kv_set(f"feedback:{node.id}", feedback[-3:])
                can_retry = (
                    outcome.retryable
                    and attempt < node.max_attempts
                    and self.budget.usage.retries < self.config.budgets.max_retries_per_run
                    and self._stop_reason is None
                )
                if not can_retry:
                    break
                self.budget.add_retry()
                self._transition(node.id, NodeState.RETRYING, attempt, "scheduling retry")
                delay = self.config.retry.delay(attempt, self._rng)
                self.audit.append(
                    "retry_scheduled", node_id=node.id, attempt=attempt, delay_s=delay
                )
                await asyncio.sleep(delay)
            span.status = "ERROR"
            self._fail_node(node.id, feedback[-1] if feedback else "failed")

    async def _gates(
        self, node: TaskNode, attempt: int, phase: str, names: list[str], **kw: Any
    ) -> str:
        """Run gates; return a failure description (empty string when all passed)."""
        failures = []
        gctx = GateContext(
            node=node,
            attempt=attempt,
            context=self.context,
            workspace=self.ws,
            settings=self.config.settings,
            **kw,
        )
        for name in names:
            with self.tracer.span(f"gate:{name}", phase=phase) as span:
                result = await self.deps.gates.evaluate(name, gctx)
                span.set(passed=result.passed)
            self.store.add_gate_result(node.id, attempt, phase, result)
            self.audit.append(
                "gate", node_id=node.id, attempt=attempt, phase=phase, gate=name,
                passed=result.passed, details=result.details[:2000],
            )  # fmt: skip
            if not result.passed:
                failures.append(f"[{phase} gate {name}] {result.details}")
                if phase == "exit":
                    break  # cheap gates first; no point running tests if lint fails
        return "\n".join(failures)

    async def _attempt(self, node: TaskNode, attempt: int, feedback: list[str]) -> AttemptOutcome:
        agent = self.deps.agents.get(node.agent)
        if agent is None:
            return AttemptOutcome(
                "failed", f"no agent registered as '{node.agent}'", retryable=False
            )
        tools = (
            ToolBelt(self.ws, agent=agent.name, permissions=agent.permissions) if self.ws else None
        )
        actx = AgentContext(
            run_id=self.run_id,
            node=node,
            attempt=attempt,
            context=self.context,
            run_dir=self.run_dir,
            feedback=list(feedback),
            tools=tools,
            llm=self._llm_for(node.id),
            settings={
                **self.config.settings,
                "ambiguity_threshold": self.config.ambiguity_threshold,
            },
        )
        try:
            with self.tracer.span(f"agent:{agent.name}"):
                result = await agent.run(actx)
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            self.audit.append("agent_error", node_id=node.id, attempt=attempt, error=repr(exc))
            return AttemptOutcome("failed", f"agent {agent.name} raised {exc!r}\n{tb}")

        parents = self.decisions.latest_for_nodes(node.depends_on) or [
            self.store.kv_get("root_decision")
        ]
        decision = self.decisions.record(
            node_id=node.id,
            agent=agent.name,
            inputs={"node": node.model_dump(mode="json"), "attempt": attempt, "feedback": feedback},
            rationale=result.rationale or result.summary,
            alternatives=result.alternatives,
            parents=[p for p in parents if p],
            outcome=result.summary,
        )
        self.audit.append(
            "decision", node_id=node.id, decision_id=decision.decision_id, agent=agent.name,
            rationale=decision.rationale[:1000], parents=decision.parent_decision_ids,
        )  # fmt: skip

        # ---------------------------------------------------------- plan expansion check
        if "plan" in result.artifacts:
            problems = self._validate_plan_expansion(node, result)
            if problems:
                return AttemptOutcome("failed", f"plan failed DAG validation: {problems}")

        # ---------------------------------------------------------- apply change set
        applied: AppliedChange | None = None
        if result.changeset is not None and result.changeset.ops:
            if WRITE not in agent.permissions:
                self.audit.append("permission_denied", node_id=node.id, agent=agent.name)
                return AttemptOutcome(
                    "failed", f"{agent.name} has no write permission", retryable=False
                )
            if self.ws is None:
                return AttemptOutcome("failed", "change set produced but no workspace attached")
            try:
                applied = self.ws.apply(result.changeset, allowed_paths=node.write_paths)
            except (ApplyError, WorkspaceViolation) as exc:
                self.audit.append("apply_rejected", node_id=node.id, error=str(exc))
                return AttemptOutcome("failed", f"change set rejected: {exc}")
            self.budget.add_files(applied.files)
            self.audit.append(
                "changes_applied", node_id=node.id, attempt=attempt, files=applied.files,
                lines_added=applied.lines_added, lines_removed=applied.lines_removed,
                diff_sha256=applied.diff_hash,
            )  # fmt: skip
            (self.run_dir / "diffs").mkdir(exist_ok=True)
            (self.run_dir / "diffs" / f"{node.id}.attempt{attempt}.diff").write_text(
                applied.diff, encoding="utf-8"
            )

        # ---------------------------------------------------------- policies
        need_reasons: list[str] = []
        need_risk = RiskLevel.LOW
        if self.deps.policy_engine is not None:
            stage = "release" if node.type.value == "release_check" else "node"
            policy_applied = applied
            if stage == "release" and self.ws is not None:
                policy_applied = await self._run_diff()
            with self.tracer.span("policies", stage=stage):
                results = await self.deps.policy_engine.evaluate(
                    PolicyInput(node=node, applied=policy_applied, workspace=self.ws, stage=stage)
                )
            blocks = []
            for pr in results:
                self.store.add_policy_result(node.id, attempt, pr)
                if pr.outcome is not PolicyOutcome.PASS:
                    self.audit.append(
                        "policy", node_id=node.id, attempt=attempt, policy=pr.policy_id,
                        outcome=pr.outcome.value, evidence=pr.evidence[:10],
                    )  # fmt: skip
                if pr.outcome is PolicyOutcome.BLOCK:
                    blocks.append(f"{pr.policy_id}: {'; '.join(pr.evidence[:5])}")
                elif pr.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                    need_reasons.append(f"policy {pr.policy_id}: {'; '.join(pr.evidence[:3])}")
                    need_risk = RiskLevel.max(need_risk, pr.risk)
            self.audit.append(
                "policies_evaluated", node_id=node.id, attempt=attempt, stage=stage,
                total=len(results), blocked=len(blocks),
            )  # fmt: skip
            if blocks:
                return AttemptOutcome(
                    "failed", "policy violations:\n" + "\n".join(blocks), applied=applied
                )

        # ---------------------------------------------------------- exit gates
        failure = await self._gates(
            node, attempt, "exit", node.exit_criteria, result=result, applied=applied
        )
        if failure:
            return AttemptOutcome("failed", failure, applied=applied)

        # ---------------------------------------------------------- approval checkpoint
        need = self._approval_need(node, applied, result)
        if need_reasons:
            need = ApprovalNeed(
                (need.reasons if need else []) + need_reasons,
                RiskLevel.max(need.risk if need else RiskLevel.LOW, need_risk),
            )
        kind = str(result.artifacts.get("approval_request", {}).get("kind", "change"))
        if need is not None:
            gate_id = f"{node.id}#{attempt}"
            request = ApprovalRequest(
                gate_id=gate_id,
                node_id=node.id,
                risk=need.risk,
                reasons=need.reasons,
                diff_hash=applied.diff_hash if applied else sha256_json(result.artifacts),
            )
            self.store.put_approval(request)
            self.audit.append(
                "approval_requested", gate_id=gate_id, node_id=node.id, risk=need.risk.value,
                reasons=need.reasons, diff_hash=request.diff_hash,
            )  # fmt: skip
            approved, comment, approver, auto = await self._seek_approval(request)
            pending = _Pending(
                result=result,
                applied=_applied_dict(applied),
                decision_id=decision.decision_id,
                gate_id=gate_id,
                approval_kind=kind,
            )
            if approved is None:
                self.store.update_node(node.id, pending_json=_pending_json(pending))
                self._transition(node.id, NodeState.AWAITING_APPROVAL, attempt, need.reasons[0])
                return AttemptOutcome("awaiting")
            self._record_decision(request, approved, approver, comment, auto)
            if not approved:
                return AttemptOutcome(
                    "failed", f"rejected by {approver}: {comment}", applied=applied
                )
            if kind == "clarification" and comment and not auto:
                self._apply_clarification(result, approver, comment)
        await self._finalize(node, attempt, result, applied, decision.decision_id)
        return AttemptOutcome("succeeded")

    def _approval_need(
        self, node: TaskNode, applied: AppliedChange | None, result: AgentResult
    ) -> ApprovalNeed | None:
        reasons: list[str] = []
        risk = RiskLevel.LOW
        if self.deps.autonomy is not None:
            run_risk = RiskLevel(self.store.run()["risk"])
            auto_need = evaluate_autonomy(self.deps.autonomy, node, applied, run_risk=run_risk)
            if auto_need:
                reasons += auto_need.reasons
                risk = auto_need.risk
        agent_req = result.artifacts.get("approval_request")
        if isinstance(agent_req, dict) and agent_req.get("reasons"):
            reasons += [str(r) for r in agent_req["reasons"]]
            risk = RiskLevel.max(risk, RiskLevel(agent_req.get("risk", "medium")))
        return ApprovalNeed(reasons, risk) if reasons else None

    async def _seek_approval(self, request: ApprovalRequest) -> tuple[bool | None, str, str, bool]:
        never = self.deps.autonomy.never_auto_approve if self.deps.autonomy else {RiskLevel.HIGH}
        auto_ok, why = self.auto.decide(request.risk, never)
        if auto_ok:
            if request.risk in never:
                self.audit.append(
                    "high_risk_override", gate_id=request.gate_id, flag="--i-understand-high-risk",
                    operator=approver_identity(),
                )  # fmt: skip
            return True, why, f"auto-approver ({approver_identity()})", True
        if self.config.interactive and self.deps.prompter is not None:
            approved, comment = await self.deps.prompter(request)
            return approved, comment, approver_identity(), False
        self.audit.append("approval_blocked", gate_id=request.gate_id, why=why)
        return None, why, "", False

    def _record_decision(
        self, request: ApprovalRequest, approved: bool, approver: str, comment: str, auto: bool
    ) -> ApprovalRequest:
        decided = decide(request, approved=approved, approver=approver, comment=comment, auto=auto)
        self.store.put_approval(decided)
        self.audit.append(
            "approval_decided", gate_id=request.gate_id, node_id=request.node_id,
            status=decided.status.value, approver=approver, comment=comment, auto=auto,
            risk=request.risk.value, diff_hash=request.diff_hash,
        )  # fmt: skip
        return decided

    def _apply_clarification(self, result: AgentResult, approver: str, comment: str) -> None:
        spec = result.artifacts.get("spec")
        if isinstance(spec, dict):
            spec.setdefault("assumptions", []).append(f"Clarified by {approver}: {comment}")

    async def _finalize(
        self,
        node: TaskNode,
        attempt: int,
        result: AgentResult,
        applied: AppliedChange | None,
        decision_id: str,
    ) -> None:
        files = applied.files if applied else []
        commit = None
        if self.ws is not None and files:
            commit = await self.ws.commit(
                files,
                f"{node.id}: {result.summary[:72]}\n\nrun: {self.run_id}\nnode: {node.id}\n"
                f"decision: {decision_id}\nagent: {node.agent}",
            )
            self._update_manifest(files, node.id)
        for key, value in result.artifacts.items():
            if key == "approval_request":
                continue
            self.context.publish(key, value, node_id=node.id, paths=files)
        if files:
            self.context.publish(
                f"changes:{node.id}",
                {
                    "files": files,
                    "commit": commit,
                    "diff_sha256": applied.diff_hash if applied else None,
                },
                node_id=node.id,
                paths=files,
            )
        if "plan" in result.artifacts:
            self._expand_plan(node, result)
        if "spec" in result.artifacts:
            spec = RequirementSpec.model_validate(result.artifacts["spec"])
            self._raise_run_risk(spec.risk)
        self.store.update_node(node.id, pending_json=None, error=None)
        self._transition(node.id, NodeState.SUCCEEDED, attempt, result.summary[:300])
        self._apply_scripted_events(node.id)

    # ================================================================== approvals on resume

    async def _process_decided_approvals(self) -> None:
        """Finish nodes whose approval was decided out-of-band (CLI approve/reject)."""
        rows = {r["node_id"]: r for r in self.store.node_rows()}
        for row in rows.values():
            if (
                NodeState(row["state"]) is not NodeState.AWAITING_APPROVAL
                or not row["pending_json"]
            ):
                continue
            pending = _load_pending(row["pending_json"])
            approval = self.store.approval(pending.gate_id)
            if approval is None or approval.status is ApprovalStatus.PENDING:
                continue
            node = TaskNode.model_validate_json(row["spec_json"])
            attempt = int(row["attempts"])
            applied = _applied_from_dict(pending.applied)
            if approval.status is ApprovalStatus.APPROVED:
                if applied is not None and self.ws is not None and row["checkpoint"]:
                    current = await self.ws.diff_since(row["checkpoint"], applied.files)
                    if not _same_change(current, applied):
                        self.audit.append(
                            "approval_invalidated",
                            gate_id=pending.gate_id,
                            reason="workspace changed after approval request",
                        )
                        await self._rollback_paths(node, row["checkpoint"], applied.files)
                        self._transition(node.id, NodeState.ROLLED_BACK, attempt, "diff changed")
                        self._transition(node.id, NodeState.RETRYING, attempt, "re-run")
                        self._transition(node.id, NodeState.READY, attempt, "re-run after drift")
                        continue
                if pending.approval_kind == "clarification" and approval.comment:
                    self._apply_clarification(
                        pending.result, approval.approver or "?", approval.comment
                    )
                self._transition(node.id, NodeState.RUNNING, attempt, "approved out-of-band")
                await self._finalize(node, attempt, pending.result, applied, pending.decision_id)
            else:
                if applied is not None and self.ws is not None and row["checkpoint"]:
                    await self._rollback_paths(node, row["checkpoint"], applied.files)
                    self._transition(node.id, NodeState.ROLLED_BACK, attempt, "rejected")
                feedback = list(self.store.kv_get(f"feedback:{node.id}", []))
                feedback.append(f"rejected by {approval.approver}: {approval.comment}")
                self.store.kv_set(f"feedback:{node.id}", feedback[-3:])
                if attempt < node.max_attempts:
                    self._transition(node.id, NodeState.RETRYING, attempt, "retry after rejection")
                    self._transition(
                        node.id, NodeState.READY, attempt, "re-queued with reviewer feedback"
                    )
                else:
                    self._fail_node(node.id, f"rejected by {approval.approver}: {approval.comment}")
        if self._pending_replan_gate is None:
            gate = self.store.kv_get("pending_replan_gate")
            if gate:
                approval = self.store.approval(gate)
                if approval and approval.status is ApprovalStatus.APPROVED:
                    self.store.kv_set("pending_replan_gate", None)
                elif approval and approval.status is ApprovalStatus.REJECTED:
                    self._stop_reason = f"re-plan {gate} rejected by {approval.approver}"
                    self.audit.append("safe_stop", reason=self._stop_reason)
                else:
                    self._pending_replan_gate = gate

    # ================================================================== plan expansion

    def _validate_plan_expansion(self, node: TaskNode, result: AgentResult) -> list[str]:
        try:
            plan = PlanDAG.model_validate(result.artifacts["plan"])
            merged = self._merged_nodes(node, plan)
            dag.validate(merged, external_inputs=self._known_artifact_keys())
        except (ValueError, dag.DAGValidationError) as exc:
            return [str(exc)]
        return []

    def _known_artifact_keys(self) -> set[str]:
        keys = {a.key for a in self.store.artifacts()}
        for n in self.store.nodes():
            keys.update(n.outputs)
        return keys

    def _merged_nodes(self, plan_node: TaskNode, plan: PlanDAG) -> list[TaskNode]:
        existing = self.store.nodes()
        existing_ids = {n.id for n in existing}
        added = []
        for planned in plan.nodes:
            if planned.id in existing_ids:
                raise ValueError(f"planned node id collides with existing node {planned.id}")
            if not planned.depends_on:
                planned = planned.model_copy(update={"depends_on": [plan_node.id]})  # noqa: PLW2901
            added.append(planned)
        return [*existing, *added]

    def _expand_plan(self, plan_node: TaskNode, result: AgentResult) -> None:
        plan = PlanDAG.model_validate(result.artifacts["plan"])
        merged = self._merged_nodes(plan_node, plan)
        version = int(self.store.run()["plan_version"]) + 1
        existing = {n.id for n in self.store.nodes()}
        for n in merged:
            if n.id not in existing:
                self.store.upsert_node(n, plan_version=version)
        self.store.set_run_fields(plan_version=version)
        self._raise_run_risk(plan.risk)
        (self.run_dir / "plan.mmd").write_text(dag.to_mermaid(merged), encoding="utf-8")
        self.audit.append(
            "plan_expanded", plan_version=version, nodes=[n.id for n in plan.nodes],
            risk=plan.risk.value, generations=dag.generations(merged),
        )  # fmt: skip

    def _raise_run_risk(self, risk: RiskLevel) -> None:
        current = RiskLevel(self.store.run()["risk"])
        new = RiskLevel.max(current, risk)
        if new is not current:
            self.store.set_run_fields(risk=new.value)

    # ================================================================== rollback & manifest

    async def _rollback_paths(
        self, node: TaskNode, checkpoint: str, files: list[str] | None = None
    ) -> list[str]:
        assert self.ws is not None  # noqa: S101 - callers check
        if files is None:
            diff = await self.ws.run(["git", "status", "--porcelain"], timeout_s=30)
            files = []
            for line in diff.stdout.splitlines():
                rel = line[3:].strip()
                if any(
                    dag.paths_overlap(rel, p) or rel.startswith(p.rstrip("*"))
                    for p in node.write_paths
                ):
                    files.append(rel)
        restored = await self.ws.rollback(checkpoint, files)
        return restored

    def _update_manifest(self, files: list[str], node_id: str) -> None:
        assert self.ws is not None  # noqa: S101 - only called with a workspace
        manifest: dict[str, list[Any]] = self.store.kv_get("manifest", {})
        for f in files:
            manifest[f] = [sha256_file(self.ws.root / f), node_id]
        self.store.kv_set("manifest", manifest)

    async def _run_diff(self) -> AppliedChange:
        assert self.ws is not None  # noqa: S101
        base = self.store.kv_get("baseline_commit")
        diff = await self.ws.diff_since(base) if base else ""
        applied = AppliedChange(diff=diff)
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                applied.files.append(line[6:])
            elif line.startswith("+") and not line.startswith("+++"):
                applied.lines_added += 1
            elif line.startswith("-") and not line.startswith("---"):
                applied.lines_removed += 1
        return applied

    # ================================================================== scripted events

    def _apply_scripted_events(self, node_id: str) -> None:
        if not self.config.events_path:
            return
        applied: list[str] = self.store.kv_get("events_applied", [])
        events = yaml.safe_load(Path(self.config.events_path).read_text(encoding="utf-8")) or []
        for i, ev in enumerate(events):
            key = f"{i}:{ev.get('after_node')}"
            if ev.get("after_node") != node_id or key in applied:
                continue
            if ev.get("action") == "amend_requirement":
                req = Path(self.store.run()["requirement_path"])
                req.write_text(
                    req.read_text(encoding="utf-8").rstrip() + "\n\n" + ev["append"].strip() + "\n",
                    encoding="utf-8",
                )
            elif ev.get("action") == "edit_file" and self.ws is not None:
                target = self.ws.resolve(ev["path"])
                target.write_text(
                    target.read_text(encoding="utf-8") + ev["append"], encoding="utf-8"
                )
            applied.append(key)
            self.store.kv_set("events_applied", applied)
            self.audit.append(
                "scripted_event", after_node=node_id, action=ev.get("action"),
                note=ev.get("note", ""), source=self.config.events_path,
            )  # fmt: skip

    # ================================================================== dynamic re-planning

    async def _check_replan(self) -> None:
        """Detect upstream changes (requirement edited, produced files edited by a human)."""
        if self._pending_replan_gate is not None:
            return
        run = self.store.run()
        req_path = Path(run["requirement_path"])
        recorded = self.store.kv_get("requirement_hash")
        current = sha256_file(req_path)
        requirement_changed = recorded is not None and current != recorded

        drifted: dict[str, str] = {}
        if self.ws is not None:
            awaiting: set[str] = set()
            for row in self.store.node_rows():
                if row["state"] == NodeState.AWAITING_APPROVAL.value and row["pending_json"]:
                    p = _load_pending(row["pending_json"])
                    awaiting.update((p.applied or {}).get("files", []))
            manifest: dict[str, list[Any]] = self.store.kv_get("manifest", {})
            for path, (digest, producer) in manifest.items():
                if path not in awaiting and sha256_file(self.ws.root / path) != digest:
                    drifted[path] = producer
        if not requirement_changed and not drifted:
            return
        if self.deps.replanner is None:
            self.audit.append("change_detected_no_replanner", requirement=requirement_changed,
                              files=sorted(drifted))  # fmt: skip
            self.store.kv_set("requirement_hash", current)
            return
        await self._replan(req_path, requirement_changed, drifted)

    async def _replan(
        self, req_path: Path, requirement_changed: bool, drifted: dict[str, str]
    ) -> None:
        n = int(self.store.kv_get("replans", 0)) + 1
        replan_id = f"replan-{n}"
        nodes = self.store.nodes()
        states = self.store.node_states()
        old_risk = RiskLevel.max(*(x.risk for x in nodes), RiskLevel(self.store.run()["risk"]))
        assert self.deps.replanner is not None  # noqa: S101
        with self.tracer.span(f"replan:{replan_id}", node_id=replan_id):
            self.audit.append(
                "change_detected", replan_id=replan_id, requirement_changed=requirement_changed,
                drifted_files=drifted, new_requirement_sha256=sha256_file(req_path),
            )  # fmt: skip
            old_spec = self.context.typed("spec", RequirementSpec)
            new_spec = old_spec
            if requirement_changed:
                text = req_path.read_text(encoding="utf-8")
                new_spec = await self.deps.replanner.reanalyze(
                    text, old_spec, self._llm_for(replan_id)
                )
                self.context.publish(
                    "requirement", {"text": text, "path": str(req_path)}, node_id=replan_id
                )
                self.context.publish("spec", new_spec, node_id=replan_id)
            changed_nodes = sorted(set(drifted.values()))
            delta = await self.deps.replanner.delta(
                old_spec=old_spec, new_spec=new_spec, nodes=nodes, states=states,
                changed_nodes=changed_nodes, llm=self._llm_for(replan_id),
            )  # fmt: skip
            new_nodes = dag.apply_delta(nodes, delta)
            dag.validate(new_nodes, external_inputs=self._known_artifact_keys())

        version = int(self.store.run()["plan_version"]) + 1
        removed = set(delta.remove)
        for node in new_nodes:
            self.store.upsert_node(node, plan_version=version)
        invalidate: set[str] = set(delta.rerun) | {c.id for c in delta.modify}
        for changed in changed_nodes:
            invalidate |= dag.descendants(new_nodes, changed)
        for nid in sorted(invalidate):
            for d in sorted({nid} | dag.descendants(new_nodes, nid)):
                if states.get(d) in {
                    NodeState.SUCCEEDED,
                    NodeState.FAILED,
                    NodeState.BLOCKED,
                    NodeState.SKIPPED,
                }:
                    self._transition(d, NodeState.PENDING, 0, f"invalidated by {replan_id}")
                    self.store.update_node(d, attempts=0)
        for rid in removed:
            if states.get(rid) in {NodeState.PENDING, NodeState.READY}:
                self._transition(rid, NodeState.SKIPPED, 0, f"removed by {replan_id}")
        new_risk = RiskLevel.max(*(x.risk for x in new_nodes if x.id not in removed),
                                 new_spec.risk if new_spec else RiskLevel.LOW)  # fmt: skip
        self.store.set_run_fields(plan_version=version)
        self.store.kv_set("requirement_hash", sha256_file(req_path))
        self.store.kv_set("replans", n)
        if self.ws is not None and drifted:
            self._update_manifest(sorted(drifted), "human-edit")
        parent = self.decisions.latest_for_nodes(["plan"])
        decision = self.decisions.record(
            node_id=replan_id, agent="Planner", inputs=delta.model_dump(mode="json"),
            rationale=delta.rationale, parents=parent or [self.store.kv_get("root_decision")],
            outcome=f"+{len(delta.add)} nodes, -{len(delta.remove)}, ~{len(delta.modify)}, "
                    f"rerun {sorted(invalidate)}",
        )  # fmt: skip
        (self.run_dir / f"plan.v{version}.mmd").write_text(
            dag.to_mermaid(new_nodes), encoding="utf-8"
        )
        self.audit.append(
            "replanned", replan_id=replan_id, plan_version=version,
            decision_id=decision.decision_id,
            added=[x.id for x in delta.add], removed=delta.remove,
            modified=[c.id for c in delta.modify], invalidated=sorted(invalidate),
            old_risk=old_risk.value, new_risk=new_risk.value,
        )  # fmt: skip
        self._raise_run_risk(new_risk)
        if new_risk.rank > old_risk.rank:
            request = ApprovalRequest(
                gate_id=replan_id,
                node_id=replan_id,
                risk=new_risk,
                reasons=[
                    f"re-plan raised run risk {old_risk.value} -> {new_risk.value}: "
                    f"{delta.rationale}"
                ],
                diff_hash=sha256_json(delta.model_dump(mode="json")),
            )
            self.store.put_approval(request)
            self.audit.append("approval_requested", gate_id=replan_id, node_id=replan_id,
                              risk=new_risk.value, reasons=request.reasons)  # fmt: skip
            approved, comment, approver, auto = await self._seek_approval(request)
            if approved is None:
                self._pending_replan_gate = replan_id
                self.store.kv_set("pending_replan_gate", replan_id)
                return
            self._record_decision(request, approved, approver, comment, auto)
            if not approved:
                self._stop_reason = f"re-plan {replan_id} rejected"
                self.audit.append("safe_stop", reason=self._stop_reason)

    # ================================================================== external commands

    def approve(
        self, gate_id: str, *, approved: bool, comment: str, approver: str | None = None
    ) -> ApprovalRequest:
        request = self.store.approval(gate_id)
        if request is None:
            raise KeyError(f"no approval gate '{gate_id}' in run {self.run_id}")
        if request.status is not ApprovalStatus.PENDING:
            raise ValueError(f"gate {gate_id} already {request.status.value}")
        return self._record_decision(
            request, approved, approver or approver_identity(), comment, False
        )


# ====================================================================== helpers


def _applied_dict(applied: AppliedChange | None) -> dict[str, Any] | None:
    if applied is None:
        return None
    return {
        "files": applied.files,
        "created": applied.created,
        "deleted": applied.deleted,
        "diff": applied.diff,
        "lines_added": applied.lines_added,
        "lines_removed": applied.lines_removed,
    }


def _applied_from_dict(data: dict[str, Any] | None) -> AppliedChange | None:
    return AppliedChange(**data) if data else None


def _pending_json(p: _Pending) -> str:
    return json.dumps(
        {
            "result": p.result.model_dump(mode="json"),
            "applied": p.applied,
            "decision_id": p.decision_id,
            "gate_id": p.gate_id,
            "approval_kind": p.approval_kind,
        }
    )


def _load_pending(raw: str) -> _Pending:
    data = json.loads(raw)
    return _Pending(
        result=AgentResult.model_validate(data["result"]),
        applied=data["applied"],
        decision_id=data["decision_id"],
        gate_id=data["gate_id"],
        approval_kind=data.get("approval_kind", "change"),
    )


def _same_change(git_diff: str, applied: AppliedChange) -> bool:
    """Compare a git diff with the recorded change by their added/removed line multisets."""

    def lines(text: str, sign: str) -> list[str]:
        return sorted(
            ln[1:]
            for ln in text.splitlines()
            if ln.startswith(sign) and not ln.startswith(sign * 3)
        )

    return lines(git_diff, "+") == lines(applied.diff, "+") and lines(git_diff, "-") == lines(
        applied.diff, "-"
    )
