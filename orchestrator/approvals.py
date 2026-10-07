"""Controlled autonomy: decide what needs a human, and record every approval decision.

The autonomy policy (``policies/autonomy.yaml``) lists actions agents may take alone and
the triggers that require approval. High-risk gates are never auto-approved unless the run
was started with the explicit ``--i-understand-high-risk`` flag, and that use is audited.
"""

from __future__ import annotations

import fnmatch
import getpass
import os
import re
import subprocess  # nosec B404 - fixed argv, used only to read git identity
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orchestrator.models import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    TaskNode,
    TaskType,
    utcnow,
)
from orchestrator.workspace import AppliedChange


@dataclass
class ApprovalTrigger:
    id: str
    description: str
    risk: RiskLevel
    paths: list[str] = field(default_factory=list)
    content_patterns: list[str] = field(default_factory=list)
    escalate_patterns: list[str] = field(default_factory=list)
    on_delete: bool = False
    max_lines: int | None = None
    node_types: list[str] = field(default_factory=list)


@dataclass
class AutonomyPolicy:
    autonomous: list[str]
    triggers: list[ApprovalTrigger]
    never_auto_approve: set[RiskLevel]

    @classmethod
    def load(cls, path: Path) -> AutonomyPolicy:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = [
            ApprovalTrigger(
                id=t["id"],
                description=t.get("description", ""),
                risk=RiskLevel(t.get("risk", "medium")),
                paths=t.get("paths", []),
                content_patterns=t.get("content_patterns", []),
                escalate_patterns=t.get("escalate_patterns", []),
                on_delete=bool(t.get("on_delete", False)),
                max_lines=t.get("max_lines"),
                node_types=t.get("node_types", []),
            )
            for t in data.get("requires_approval", [])
        ]
        never = {RiskLevel(r) for r in data.get("never_auto_approve", ["high"])}
        return cls(
            autonomous=data.get("autonomous", []), triggers=triggers, never_auto_approve=never
        )


@dataclass
class ApprovalNeed:
    reasons: list[str]
    risk: RiskLevel


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def _upgrade_section(lines: list[str]) -> str:
    """Only DDL in ``upgrade()`` signals destructiveness; drops in ``downgrade()`` are expected."""
    out: list[str] = []
    inside = not any(ln.startswith("def upgrade") for ln in lines)
    for ln in lines:
        if ln.startswith("def upgrade"):
            inside = True
        elif ln.startswith("def ") and not ln.startswith("def upgrade"):
            inside = False
        elif inside:
            out.append(ln)
    return "\n".join(out)


def evaluate_autonomy(
    policy: AutonomyPolicy,
    node: TaskNode,
    applied: AppliedChange | None,
    *,
    run_risk: RiskLevel = RiskLevel.LOW,
) -> ApprovalNeed | None:
    """Return why ``node``'s change needs approval (and at what risk), or ``None``."""
    reasons: list[str] = []
    risk = RiskLevel.LOW
    added = applied.added_lines() if applied else []
    for trig in policy.triggers:
        hit: str | None = None
        trig_risk = trig.risk
        if trig.node_types and node.type.value in trig.node_types:
            hit = f"{node.type.value} node"
            if node.type is TaskType.RELEASE_CHECK:
                trig_risk = RiskLevel.max(trig.risk, run_risk)
        if applied is not None:
            touched = [f for f in applied.files if trig.paths and _matches(f, trig.paths)]
            if touched and not trig.content_patterns:
                hit = ", ".join(touched)
            if touched and trig.content_patterns:
                lines = [ln for p, ln in added if p in touched]
                if any(re.search(cp, ln) for cp in trig.content_patterns for ln in lines):
                    hit = ", ".join(touched)
            if touched and trig.escalate_patterns:
                body = "\n".join(
                    _upgrade_section([ln for p, ln in added if p == f]) for f in touched
                )
                if any(re.search(ep, body) for ep in trig.escalate_patterns):
                    trig_risk = RiskLevel.HIGH
                    hit = f"{hit} (destructive operation detected)"
            if trig.on_delete and applied.deleted:
                hit = f"deletes {applied.deleted}"
            if trig.max_lines is not None and applied.lines_changed > trig.max_lines:
                hit = f"{applied.lines_changed} lines changed > {trig.max_lines}"
        if hit:
            reasons.append(f"{trig.id}: {trig.description} [{hit}]")
            risk = RiskLevel.max(risk, trig_risk)
    if node.risk is RiskLevel.HIGH and node.is_write:
        reasons.append(f"node_risk: planner classified {node.id} as high risk")
        risk = RiskLevel.HIGH
    return ApprovalNeed(reasons, risk) if reasons else None


def approver_identity() -> str:
    explicit = os.environ.get("ORCH_APPROVER")
    if explicit:
        return explicit
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],  # noqa: S607 - git resolved from PATH by design
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return getpass.getuser()


@dataclass
class AutoApprovePolicy:
    levels: set[RiskLevel] = field(default_factory=set)
    allow_high_risk: bool = False

    @classmethod
    def parse(cls, spec: str | None, *, allow_high_risk: bool = False) -> AutoApprovePolicy:
        levels = {RiskLevel(s.strip()) for s in (spec or "").split(",") if s.strip()}
        return cls(levels=levels, allow_high_risk=allow_high_risk)

    def decide(self, risk: RiskLevel, never: set[RiskLevel]) -> tuple[bool, str]:
        if risk in never:
            if self.allow_high_risk:
                return True, "auto-approved via explicit --i-understand-high-risk flag"
            return False, f"{risk.value}-risk gates are never auto-approved"
        if risk in self.levels:
            levels = ",".join(sorted(lv.value for lv in self.levels))
            return True, f"auto-approved by --auto-approve={levels}"
        return False, f"{risk.value} risk not in auto-approve set"


def decide(
    request: ApprovalRequest, *, approved: bool, approver: str, comment: str, auto: bool
) -> ApprovalRequest:
    return request.model_copy(
        update={
            "status": ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
            "decided_at": utcnow(),
            "approver": approver,
            "comment": comment,
            "auto": auto,
        }
    )
