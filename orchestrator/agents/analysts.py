"""RequirementAnalyst and CodebaseAnalyst."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from orchestrator.agents.base import READ, RUN, Agent, AgentContext
from orchestrator.agents.contracts import ImpactNarrative
from orchestrator.analysis import codebase
from orchestrator.models import AgentResult, RequirementSpec, RiskLevel
from orchestrator.providers.base import LLMClient

ANALYST_SYSTEM = """You are the RequirementAnalyst in a governed software delivery pipeline.
Normalise the requirement into a RequirementSpec: goal, functional and non-functional requirements,
testable Given/When/Then acceptance criteria (with the test ids that will verify them), constraints,
ambiguities (vague terms, with a clarifying question and a documented default assumption each),
assumptions, open questions, an ambiguity_score in [0,1], a risk level, and the capability ids
involved. Never invent requirements that are not implied by the text; prefer raising a question."""

IMPACT_SYSTEM = """You are the CodebaseAnalyst. Given static-analysis facts about a Python/FastAPI
codebase (import graph, routes, tables, test mapping, blast radius), summarise the impact of the
requested change, classify its risk and list concrete recommendations."""


class RequirementAnalyst(Agent):
    name = "RequirementAnalyst"
    permissions = frozenset({READ})
    description = (
        "Normalises free text / YAML requirements into a RequirementSpec; flags ambiguity."
    )

    async def analyze(
        self,
        text: str,
        filename: str,
        previous: RequirementSpec | None,
        llm: LLMClient,
    ) -> RequirementSpec:
        prompt = (
            f"Requirement file: {filename}\n---\n{text}\n---\n"
            + (f"Previous spec version:\n{previous.model_dump_json()}\n" if previous else "")
            + "Return the RequirementSpec as JSON."
        )
        return await llm.structured(
            role=self.name,
            task="analyze",
            system=ANALYST_SYSTEM,
            prompt=prompt,
            schema=RequirementSpec,
            context={
                "text": text,
                "filename": filename,
                "previous": previous.model_dump(mode="json") if previous else None,
            },
        )

    async def run(self, ctx: AgentContext) -> AgentResult:
        req = ctx.context.content("requirement")
        filename = req.get("source") or Path(req["path"]).name
        spec = await self.analyze(req["text"], filename, ctx.spec, ctx.require_llm())
        threshold = float(ctx.settings.get("ambiguity_threshold", 0.35))
        artifacts: dict[str, Any] = {"spec": spec.model_dump(mode="json")}
        lines = [f"# Clarification for {spec.id} (ambiguity score {spec.ambiguity_score})", ""]
        for a in spec.ambiguities:
            lines += [
                f"- **{a.term}** ({a.impact.value}): {a.question}",
                f"  - default: {a.default_assumption}",
            ]
        lines += ["", "## Open questions", *[f"- {q}" for q in spec.open_questions or ["none"]]]
        (ctx.run_dir / "clarification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if spec.ambiguity_score > threshold:
            artifacts["approval_request"] = {
                "kind": "clarification",
                "risk": RiskLevel.MEDIUM.value,
                "reasons": [
                    f"clarification: ambiguity score {spec.ambiguity_score} > {threshold}; "
                    f"proceeding on {len(spec.ambiguities)} documented default assumption(s) "
                    f"(see clarification.md): "
                    + "; ".join(
                        f"{a.term} -> {a.default_assumption[:90]}" for a in spec.ambiguities
                    )
                ],
            }
        return AgentResult(
            summary=(
                f"{spec.id}: {len(spec.functional)} functional reqs, "
                f"{len(spec.acceptance_criteria)} acceptance criteria, "
                f"ambiguity {spec.ambiguity_score}, risk {spec.risk.value}"
            ),
            artifacts=artifacts,
            rationale=(
                f"Capabilities {spec.capabilities}. Ambiguities: "
                f"{[a.term for a in spec.ambiguities] or 'none'}. "
                f"Assumptions: {len(spec.assumptions)}."
            ),
            alternatives=["Block until a human answers every question (safer, slower)"]
            if spec.ambiguities
            else [],
            risk=spec.risk,
        )


_SNAKE = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
_PERSIST_WORDS = re.compile(r"\b(stored?|persist\w*|table|column|migration|record(ed)?)\b", re.I)
_STOP = {
    "with",
    "from",
    "that",
    "this",
    "must",
    "only",
    "into",
    "when",
    "will",
    "have",
    "link",
    "links",
}


def seed_files(spec: RequirementSpec, facts: codebase.CodebaseFacts) -> list[str]:
    """Pick the modules a requirement most likely touches, by term overlap with the code index."""
    text = " ".join([spec.goal, *spec.functional]).lower()
    terms = {t.rstrip("s") for t in re.findall(r"[a-z]{4,}", text)} - _STOP
    scores: dict[str, int] = {}
    for mod in facts.modules:
        hay = " ".join(
            [mod.replace(".", " "), *(s["name"].lower() for s in facts.symbols.get(mod, []))]
            + [r["path"] for r in facts.routes if r["endpoint"].startswith(mod + ".")]
        ).lower()
        scores[mod] = sum(1 for t in terms if t in hay)
    ranked = sorted((s, m) for m, s in scores.items() if s >= 2)
    return [m.replace(".", "/") + ".py" for _, m in reversed(ranked[-8:])]


def migrations_needed(spec: RequirementSpec, facts: codebase.CodebaseFacts) -> bool:
    if not facts.tables:
        return False
    text = " ".join([spec.goal, *spec.functional, *spec.non_functional])
    columns = {c for cols in facts.tables.values() for c in cols} | set(facts.settings)
    new_fields = {f for f in _SNAKE.findall(text) if f not in columns}
    return bool(new_fields) or bool(_PERSIST_WORDS.search(text) and "analytics" in text.lower())


class CodebaseAnalyst(Agent):
    name = "CodebaseAnalyst"
    permissions = frozenset({READ, RUN})
    description = "Real static analysis (AST import graph, routes, models, test mapping) -> impact."

    async def run(self, ctx: AgentContext) -> AgentResult:
        tools = ctx.require_tools()
        spec = ctx.spec
        if spec is None:
            raise RuntimeError("CodebaseAnalyst needs the spec artifact")
        facts = await codebase.analyze(tools)
        seeds = seed_files(spec, facts)
        needs_migration = migrations_needed(spec, facts)
        if needs_migration and "service.models" in facts.modules:
            seeds.append("service/models.py")  # a schema change always touches the models
        result = codebase.impact(facts, seeds, migrations_needed=needs_migration)
        narrative = await ctx.require_llm().structured(
            role=self.name,
            task="impact_summary",
            system=IMPACT_SYSTEM,
            prompt=f"Facts:\n{json.dumps(result, default=str)[:12000]}\nReturn an ImpactNarrative.",
            schema=ImpactNarrative,
            context={"facts": result},
        )
        result["narrative"] = narrative.model_dump(mode="json")
        result["symbol_index"] = facts.symbols
        result["test_map"] = facts.tests
        result["routes"] = facts.routes
        (ctx.run_dir / "impact.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
        return AgentResult(
            summary=narrative.summary,
            artifacts={"impact": result},
            rationale=(
                f"Seeds {result['seeds']} -> {len(result['impacted_modules'])} impacted modules "
                f"via the reverse import graph; routes from {result['route_source']}; "
                f"migrations_needed={result['migrations_needed']}; "
                f"blast radius {result['blast_radius']}."
            ),
            alternatives=["grep-based impact estimate (cheaper, misses transitive importers)"],
            risk=narrative.risk,
        )
