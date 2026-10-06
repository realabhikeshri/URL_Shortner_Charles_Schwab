"""Pluggable policy engine.

Policies live in ``policies/*.yaml``. Each entry names a registered check, a severity
(``block`` | ``require_approval`` | ``warn``), the stages it applies to (``node`` and/or
``release``) and check-specific params. Checks return violation messages; the engine turns
them into :class:`PolicyResult` records that feed gates, approvals and the audit trail.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orchestrator.models import PolicyOutcome, PolicyResult, RiskLevel, TaskNode
from orchestrator.workspace import AppliedChange, Workspace


@dataclass
class PolicyInput:
    node: TaskNode
    applied: AppliedChange | None
    workspace: Workspace | None
    stage: str = "node"
    extra: dict[str, Any] = field(default_factory=dict)


CheckFn = Callable[[PolicyInput, dict[str, Any]], Awaitable[list[str]]]
CHECKS: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        CHECKS[name] = fn
        return fn

    return deco


@dataclass
class PolicyRule:
    id: str
    category: str
    check: str
    severity: PolicyOutcome
    description: str
    stages: list[str]
    risk: RiskLevel
    params: dict[str, Any]


class PolicyEngine:
    def __init__(self, rules: list[PolicyRule]) -> None:
        unknown = [r.check for r in rules if r.check not in CHECKS]
        if unknown:
            raise ValueError(f"policies reference unknown checks: {unknown}")
        self.rules = rules

    @classmethod
    def from_dir(cls, directory: Path) -> PolicyEngine:
        # Import for side effect: registers the built-in checks.
        from orchestrator.policy import checks  # noqa: F401, PLC0415

        rules: list[PolicyRule] = []
        for path in sorted(directory.glob("*.yaml")):
            data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            category = data.get("category")
            if not category:
                continue  # e.g. autonomy.yaml is consumed by the approval service
            for item in data.get("policies", []):
                rules.append(
                    PolicyRule(
                        id=item["id"],
                        category=category,
                        check=item["check"],
                        severity=PolicyOutcome(item.get("severity", "block")),
                        description=item.get("description", ""),
                        stages=item.get("stages", ["node"]),
                        risk=RiskLevel(item.get("risk", "medium")),
                        params=item.get("params", {}),
                    )
                )
        return cls(rules)

    async def evaluate(self, inp: PolicyInput) -> list[PolicyResult]:
        results: list[PolicyResult] = []
        for rule in self.rules:
            if inp.stage not in rule.stages:
                continue
            try:
                violations = await CHECKS[rule.check](inp, rule.params)
            except Exception as exc:  # a broken check must fail closed, not crash the run
                violations = [f"policy check crashed: {exc!r}"]
            if violations:
                results.append(
                    PolicyResult(
                        policy_id=rule.id,
                        category=rule.category,
                        outcome=rule.severity,
                        message=f"{rule.description}: {len(violations)} violation(s)",
                        risk=rule.risk,
                        evidence=violations[:20],
                    )
                )
            else:
                results.append(
                    PolicyResult(
                        policy_id=rule.id,
                        category=rule.category,
                        outcome=PolicyOutcome.PASS,
                        message=rule.description,
                        risk=RiskLevel.LOW,
                    )
                )
        return results
