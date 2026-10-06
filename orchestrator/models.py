"""Typed domain model shared by every orchestrator component.

Everything that crosses a component boundary (agent output, persisted state, audit payloads)
is a Pydantic model so it is validated on the way in and serialisable on the way out.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- enums


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    @classmethod
    def max(cls, *levels: RiskLevel) -> RiskLevel:
        return max(levels, key=lambda r: r.rank, default=cls.LOW)


class TaskType(StrEnum):
    INTAKE = "intake"
    ANALYSIS = "analysis"
    PLAN = "plan"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST = "test"
    DOCUMENT = "document"
    REVIEW = "review"
    RELEASE_CHECK = "release_check"


WRITE_TASK_TYPES = frozenset(
    {TaskType.DESIGN, TaskType.IMPLEMENT, TaskType.TEST, TaskType.DOCUMENT}
)


class NodeState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    ROLLED_BACK = "ROLLED_BACK"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


TERMINAL_NODE_STATES = frozenset(
    {NodeState.SUCCEEDED, NodeState.FAILED, NodeState.SKIPPED, NodeState.BLOCKED}
)
DONE_OK_STATES = frozenset({NodeState.SUCCEEDED, NodeState.SKIPPED})

# Legal node transitions. The engine refuses anything else, which keeps the state machine honest.
NODE_TRANSITIONS: dict[NodeState, frozenset[NodeState]] = {
    NodeState.PENDING: frozenset({NodeState.READY, NodeState.BLOCKED, NodeState.SKIPPED}),
    NodeState.READY: frozenset(
        {NodeState.RUNNING, NodeState.BLOCKED, NodeState.SKIPPED, NodeState.PENDING}
    ),
    NodeState.RUNNING: frozenset(
        {
            NodeState.SUCCEEDED,
            NodeState.FAILED,
            NodeState.AWAITING_APPROVAL,
            NodeState.ROLLED_BACK,
            NodeState.RETRYING,
            NodeState.READY,  # crash recovery on resume
        }
    ),
    NodeState.AWAITING_APPROVAL: frozenset(
        {
            NodeState.SUCCEEDED,
            NodeState.ROLLED_BACK,
            NodeState.FAILED,
            NodeState.RUNNING,
            NodeState.RETRYING,
        }
    ),
    NodeState.ROLLED_BACK: frozenset({NodeState.RETRYING, NodeState.FAILED}),
    NodeState.RETRYING: frozenset({NodeState.RUNNING, NodeState.READY}),
    NodeState.FAILED: frozenset({NodeState.PENDING}),  # re-plan may re-open a failed node
    NodeState.SUCCEEDED: frozenset({NodeState.PENDING}),  # invalidated by re-plan
    NodeState.SKIPPED: frozenset({NodeState.PENDING}),
    NodeState.BLOCKED: frozenset({NodeState.PENDING}),
}


class RunState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SAFE_STOPPED = "SAFE_STOPPED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PolicyOutcome(StrEnum):
    PASS = "pass"  # noqa: S105 - enum value, not a password
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# --------------------------------------------------------------------------- requirements


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcceptanceCriterion(Strict):
    id: str
    given: str
    when: str
    then: str
    verified_by: list[str] = Field(
        default_factory=list, description="Test node ids / test names that prove this criterion"
    )


class Ambiguity(Strict):
    term: str
    question: str
    default_assumption: str
    impact: RiskLevel = RiskLevel.MEDIUM


class RequirementSpec(Strict):
    id: str
    title: str
    goal: str
    kind: Literal["greenfield", "brownfield", "enhancement"] = "greenfield"
    functional: list[str]
    non_functional: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    ambiguity_score: float = Field(0.0, ge=0.0, le=1.0)
    capabilities: list[str] = Field(
        default_factory=list, description="Normalised capability ids the spec asks for"
    )
    version: int = 1


# --------------------------------------------------------------------------- plan


class TaskNode(Strict):
    id: str
    type: TaskType
    title: str
    agent: str
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list, description="Artifact keys consumed")
    outputs: list[str] = Field(default_factory=list, description="Artifact keys produced")
    write_paths: list[str] = Field(
        default_factory=list, description="Workspace globs this node may write"
    )
    entry_criteria: list[str] = Field(default_factory=list)
    exit_criteria: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW
    effort_points: int = Field(1, ge=1, le=13)
    max_attempts: int = Field(3, ge=1, le=10)
    optional: bool = False
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_write(self) -> bool:
        return self.type in WRITE_TASK_TYPES and bool(self.write_paths)


class PlanDAG(Strict):
    nodes: list[TaskNode]
    rationale: str = ""
    version: int = 1

    def by_id(self) -> dict[str, TaskNode]:
        return {n.id: n for n in self.nodes}

    @property
    def risk(self) -> RiskLevel:
        return RiskLevel.max(*(n.risk for n in self.nodes))


class NodeChange(Strict):
    id: str
    changes: dict[str, Any]


class DeltaPlan(Strict):
    add: list[TaskNode] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    modify: list[NodeChange] = Field(default_factory=list)
    rerun: list[str] = Field(default_factory=list, description="Existing nodes to re-execute")
    rationale: str = ""


# --------------------------------------------------------------------------- execution records


class FileOp(Strict):
    op: Literal["write", "replace", "delete"]
    path: str
    content: str | None = None
    old: str | None = None
    new: str | None = None


class ChangeSet(Strict):
    summary: str
    ops: list[FileOp] = Field(default_factory=list)
    rationale: str = ""
    alternatives: list[str] = Field(default_factory=list)


class AgentResult(Strict):
    """What an agent hands back to the engine for one attempt."""

    summary: str
    artifacts: dict[str, Any] = Field(default_factory=dict)
    changeset: ChangeSet | None = None
    rationale: str = ""
    alternatives: list[str] = Field(default_factory=list)
    risk: RiskLevel | None = None


class Artifact(Strict):
    key: str
    version: int
    node_id: str
    content: Any
    content_hash: str
    paths: dict[str, str | None] = Field(
        default_factory=dict, description="Workspace path -> sha256 at production time"
    )
    created_at: datetime = Field(default_factory=utcnow)


class Decision(Strict):
    decision_id: str
    node_id: str
    agent: str
    inputs_hash: str
    rationale: str
    alternatives_considered: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utcnow)
    parent_decision_ids: list[str] = Field(default_factory=list)
    outcome: str = ""


class GateResult(Strict):
    gate: str
    passed: bool
    details: str = ""
    duration_s: float = 0.0


class PolicyResult(Strict):
    policy_id: str
    category: str
    outcome: PolicyOutcome
    message: str
    risk: RiskLevel = RiskLevel.LOW
    evidence: list[str] = Field(default_factory=list)


class ApprovalRequest(Strict):
    gate_id: str
    node_id: str
    risk: RiskLevel
    reasons: list[str]
    diff_hash: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    approver: str | None = None
    comment: str | None = None
    auto: bool = False


class LLMUsage(Strict):
    provider: str
    role: str
    task: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    fallback: bool = False
