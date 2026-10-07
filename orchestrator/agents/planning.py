"""Planner and Architect."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.agents.base import READ, WRITE, Agent, AgentContext
from orchestrator.agents.contracts import DesignDoc
from orchestrator.models import (
    AgentResult,
    ChangeSet,
    DeltaPlan,
    FileOp,
    NodeState,
    PlanDAG,
    RequirementSpec,
    TaskNode,
)
from orchestrator.providers.base import LLMClient

PLANNER_SYSTEM = """You are the Planner. Decompose the RequirementSpec into a DAG of typed tasks
(design, implement, test, document, review, release_check). Every node needs: id, type, agent,
depends_on, inputs (artifact keys produced upstream), outputs, write_paths (disjoint for nodes that
can run in parallel), entry/exit criteria (gate names such as compiles, lint_clean, targeted_tests,
coverage:85, migrations_apply, review_approved, release_ready), risk and effort. Maximise safe
parallelism; tests and docs should branch after implementation and join before review."""

DELTA_SYSTEM = """You are the Planner, re-planning a run in flight. Given the old and new spec and
the current nodes with their states, return a minimal DeltaPlan (add/remove/modify/rerun) that makes
the plan satisfy the new spec. Do not re-run succeeded work unless its inputs changed."""

ARCHITECT_SYSTEM = """You are the Architect. Produce a concise design for the spec and impact
analysis: components, key decisions with rationale (and rejected alternatives), data model changes,
risks and open questions."""


IMPACT_KEYS = ("impacted_modules", "tests_to_update", "migrations_needed", "blast_radius")


class Planner(Agent):
    name = "Planner"
    permissions = frozenset({READ})
    description = "Turns the spec into a validated plan DAG; produces delta plans on change."

    async def run(self, ctx: AgentContext) -> AgentResult:
        spec = ctx.spec
        impact = ctx.context.content("impact", {})
        if spec is None:
            raise RuntimeError("Planner needs the spec artifact")
        plan = await ctx.require_llm().structured(
            role=self.name,
            task="plan",
            system=PLANNER_SYSTEM,
            prompt=(
                f"Spec:\n{spec.model_dump_json()}\nImpact:\n"
                f"{json.dumps({k: impact.get(k) for k in IMPACT_KEYS})}\n"
                + (f"Previous attempt feedback:\n{ctx.feedback[-1]}\n" if ctx.feedback else "")
                + "Return the PlanDAG as JSON."
            ),
            schema=PlanDAG,
            context={"spec": spec.model_dump(mode="json"), "impact": impact},
        )
        return AgentResult(
            summary=f"plan with {len(plan.nodes)} nodes, risk {plan.risk.value}",
            artifacts={"plan": plan.model_dump(mode="json")},
            rationale=plan.rationale,
            alternatives=[
                "Single sequential pipeline: simpler, but slower and one failure stalls everything",
                "One mega implement node: fewer gates, far larger blast radius per failure",
            ],
            risk=plan.risk,
        )

    async def delta(
        self,
        *,
        old_spec: RequirementSpec | None,
        new_spec: RequirementSpec | None,
        nodes: list[TaskNode],
        states: dict[str, NodeState],
        changed_nodes: list[str],
        llm: LLMClient,
    ) -> DeltaPlan:
        context: dict[str, Any] = {
            "old_spec": old_spec.model_dump(mode="json") if old_spec else None,
            "new_spec": new_spec.model_dump(mode="json") if new_spec else None,
            "nodes": [n.model_dump(mode="json") for n in nodes],
            "states": {k: v.value for k, v in states.items()},
            "changed_nodes": changed_nodes,
        }
        return await llm.structured(
            role=self.name,
            task="delta",
            system=DELTA_SYSTEM,
            prompt=json.dumps(context, default=str)[:60000] + "\nReturn the DeltaPlan as JSON.",
            schema=DeltaPlan,
            context=context,
        )


def render_design(doc: DesignDoc, spec: RequirementSpec) -> str:
    lines = [
        f"# {doc.title}",
        "",
        f"Requirement: **{spec.id}** (v{spec.version}, risk {spec.risk.value})",
        "",
    ]
    lines += ["## Summary", "", doc.summary, "", "## Components", ""]
    lines += [f"- {c}" for c in doc.components] or ["- (none)"]
    lines += [
        "",
        "## Decisions",
        "",
        "| Decision | Rationale / rejected alternative |",
        "|---|---|",
    ]
    lines += [f"| {d.decision} | {d.rationale} |" for d in doc.decisions]
    lines += ["", "## Data model", ""] + ([f"- `{d}`" for d in doc.data_model] or ["- no change"])
    lines += ["", "## Risks and assumptions", ""] + (
        [f"- {r}" for r in doc.risks] or ["- none identified"]
    )
    lines += ["", "## Acceptance criteria", ""]
    lines += [
        f"- **{ac.id}** Given {ac.given}, when {ac.when}, then {ac.then}."
        for ac in spec.acceptance_criteria
    ]
    if doc.open_questions:
        lines += ["", "## Open questions", ""] + [f"- {q}" for q in doc.open_questions]
    return "\n".join(lines) + "\n"


class Architect(Agent):
    name = "Architect"
    permissions = frozenset({READ, WRITE})
    description = "Writes the design document for the change."

    async def run(self, ctx: AgentContext) -> AgentResult:
        spec = ctx.spec
        if spec is None:
            raise RuntimeError("Architect needs the spec artifact")
        impact = ctx.context.content("impact", {})
        doc = await ctx.require_llm().structured(
            role=self.name,
            task="design",
            system=ARCHITECT_SYSTEM,
            prompt=(
                f"Spec:\n{spec.model_dump_json()}\n"
                f"Impact narrative:\n{json.dumps(impact.get('narrative', {}))}"
            ),
            schema=DesignDoc,
            context={"spec": spec.model_dump(mode="json"), "impact": impact},
        )
        path = ctx.node.write_paths[0]
        return AgentResult(
            summary=f"design with {len(doc.components)} components, {len(doc.decisions)} decisions",
            artifacts={"design": {**doc.model_dump(mode="json"), "path": path}},
            changeset=ChangeSet(
                summary=doc.title,
                ops=[FileOp(op="write", path=path, content=render_design(doc, spec))],
            ),
            rationale="; ".join(f"{d.decision}: {d.rationale}" for d in doc.decisions)[:2000],
            alternatives=[d.rationale for d in doc.decisions if "rejected" in d.rationale.lower()],
        )
