"""Deterministic, offline provider: a rule/template-based stand-in for the LLM.

It fulfils exactly the same typed contracts as the Anthropic provider, using curated domain
knowledge (``orchestrator/blueprints``): keyword/vague-term rules for requirement analysis, a
capability catalog for planning and design, and change-set templates for code, tests and docs.
It makes the demo, CI and tests reproducible without network access or an API key.

One behaviour is deliberately scripted: an opset may declare a ``first_attempt`` variant (a
realistic but wrong fix). The provider emits it on the first attempt and only produces the correct
opset when the retry feedback contains one of the opset's ``repair_signatures`` (e.g. the name of
the failing test) - a rule-based self-repair loop that is honest about what it is.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from orchestrator.agents.contracts import (
    DesignDecision,
    DesignDoc,
    DocDraft,
    ImpactNarrative,
    ReleaseNotes,
    ReviewReport,
)
from orchestrator.blueprints import Blueprint
from orchestrator.models import (
    AcceptanceCriterion,
    Ambiguity,
    ChangeSet,
    DeltaPlan,
    NodeChange,
    PlanDAG,
    RequirementSpec,
    RiskLevel,
    TaskNode,
    TaskType,
)
from orchestrator.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    estimate_tokens,
)

BOOTSTRAP_NODE_IDS = frozenset({"intake", "analysis.impact", "plan"})


class DeterministicProvider(LLMProvider):
    name = "deterministic"

    def __init__(self, blueprint: Blueprint | None = None) -> None:
        self.bp = blueprint or Blueprint()
        self._handlers: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {
            ("RequirementAnalyst", "analyze"): self.analyze,
            ("CodebaseAnalyst", "impact_summary"): self.impact_summary,
            ("Planner", "plan"): self.plan,
            ("Planner", "delta"): self.delta,
            ("Architect", "design"): self.design,
            ("Implementer", "implement"): self.change,
            ("TestEngineer", "write_tests"): self.change,
            ("DocWriter", "document"): self.document,
            ("Reviewer", "review"): self.review,
            ("ReleaseManager", "release_notes"): self.release_notes,
        }

    async def complete(self, request: LLMRequest) -> LLMResponse:
        handler = self._handlers.get((request.role, request.task))
        if handler is None:
            raise ProviderError(f"no deterministic handler for {request.role}/{request.task}")
        result = handler(request.context)
        content = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        out_text = yaml.safe_dump(content)
        return LLMResponse(
            content=content,
            input_tokens=estimate_tokens(request.system + request.prompt),
            output_tokens=estimate_tokens(out_text),
            cost_usd=0.0,
            model="deterministic-rules-v1",
        )

    # ------------------------------------------------------------------ analysis

    def analyze(self, ctx: dict[str, Any]) -> RequirementSpec:
        text: str = ctx["text"]
        filename: str = ctx.get("filename", "requirement.txt")
        previous = ctx.get("previous")
        kb = self.bp.knowledge
        catalog = self.bp.catalog
        weights = kb["impact_weights"]

        parsed: Any = None
        if filename.endswith((".yaml", ".yml")):
            parsed = yaml.safe_load(text)
        structured = isinstance(parsed, dict) and "functional" in parsed

        if structured:
            data: dict[str, Any] = parsed
            goal = " ".join(str(data["goal"]).split())
            scan_text = " ".join(
                [goal, *data.get("functional", []), *data.get("non_functional", [])]
            )
            vague_scope = goal
        else:
            goal = " ".join(text.split())
            scan_text = text
            vague_scope = text
            data = {}

        capabilities = self._match_capabilities(scan_text, kb)
        ambiguities: list[Ambiguity] = []
        assumptions: list[str] = list(data.get("assumptions", []))
        open_questions: list[str] = []
        for term in kb["vague_terms"]:
            if re.search(term["pattern"], vague_scope, re.IGNORECASE):
                ambiguities.append(
                    Ambiguity(
                        term=term["term"],
                        question=term["question"],
                        default_assumption=" ".join(term["default_assumption"].split()),
                        impact=RiskLevel(term["impact"]),
                    )
                )
                assumptions.append(
                    f"[{term['term']}] " + " ".join(term["default_assumption"].split())
                )
                capabilities.update(term.get("implies", []))
                if term.get("open_question"):
                    open_questions.append(term["open_question"])
        resolved = 0
        for question, capability in (kb.get("answers") or {}).items():
            if question in open_questions and capability in capabilities:
                open_questions.remove(question)
                resolved += 1
                assumptions.append(
                    f"Resolved {question.split(':')[0]} by requirement text: "
                    f"{capability} is in scope."
                )

        score = sum(weights[a.impact.value] for a in ambiguities)
        score = max(score - resolved * float(kb.get("resolved_question_credit", 0.0)), 0.0)
        if structured and data.get("acceptance_criteria"):
            score = max(score - 0.15, 0.0)
        caps = self._close_capabilities(capabilities, catalog)
        risk = RiskLevel.max(
            *(RiskLevel(catalog["capabilities"][c]["risk"]) for c in caps),
            RiskLevel.MEDIUM if score > kb["ambiguity_threshold"] else RiskLevel.LOW,
        )

        if structured:
            criteria = [
                AcceptanceCriterion.model_validate(c) for c in data.get("acceptance_criteria", [])
            ]
            spec = RequirementSpec(
                id=data["id"],
                title=data["title"],
                goal=goal,
                kind=data.get("kind", "greenfield"),
                functional=list(data["functional"]),
                non_functional=list(data.get("non_functional", [])),
                acceptance_criteria=criteria,
                constraints=list(data.get("constraints", [])),
                ambiguities=ambiguities,
                assumptions=assumptions,
                open_questions=open_questions,
                risk=risk,
                ambiguity_score=round(min(score, 1.0), 3),
                capabilities=caps,
            )
        else:
            visible = [c for c in caps if not catalog["capabilities"][c].get("hidden")]
            criteria = [
                AcceptanceCriterion.model_validate(c)
                for cap in visible
                for c in catalog.get("acceptance", {}).get(cap, [])
            ]
            previous_id = previous.get("id") if isinstance(previous, dict) else None
            spec = RequirementSpec(
                id=previous_id or f"REQ-{Path(filename).stem.upper()}",
                title=(previous or {}).get("title") or goal.split(".")[0][:80],
                goal=goal,
                kind="enhancement",
                functional=[catalog["capabilities"][c]["title"] for c in visible],
                non_functional=[a.default_assumption for a in ambiguities],
                acceptance_criteria=criteria,
                constraints=[
                    "Existing public endpoints keep their contracts unless a requirement "
                    "says otherwise."
                ],
                ambiguities=ambiguities,
                assumptions=assumptions,
                open_questions=open_questions,
                risk=risk,
                ambiguity_score=round(min(score, 1.0), 3),
                capabilities=caps,
            )
        if isinstance(previous, dict):
            spec.version = int(previous.get("version", 1)) + 1
        return spec

    @staticmethod
    def _match_capabilities(text: str, kb: dict[str, Any]) -> set[str]:
        found: set[str] = set()
        for cap, patterns in kb["capability_keywords"].items():
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                found.add(cap)
        return found

    @staticmethod
    def _close_capabilities(caps: set[str], catalog: dict[str, Any]) -> list[str]:
        closed = set(caps)
        frontier = list(caps)
        while frontier:
            cap = frontier.pop()
            for req in catalog["capabilities"].get(cap, {}).get("requires", []):
                if req not in closed:
                    closed.add(req)
                    frontier.append(req)
        return [c for c in catalog["order"] if c in closed]

    def impact_summary(self, ctx: dict[str, Any]) -> ImpactNarrative:
        facts = ctx["facts"]
        score = float(facts.get("blast_radius", 0.0))
        risk = (
            RiskLevel.HIGH if score >= 0.6 else RiskLevel.MEDIUM if score >= 0.25 else RiskLevel.LOW
        )
        if not facts.get("modules"):
            return ImpactNarrative(
                summary="Empty workspace: greenfield build, no existing code is impacted.",
                risk=RiskLevel.LOW,
                recommendations=["Establish migrations, tests and CI gates from the first change."],
            )
        recs = []
        if facts.get("migrations_needed"):
            recs.append(
                "Schema change required: ship a reversible migration and gate it on human approval."
            )
        if facts.get("tests_to_update"):
            recs.append(f"Re-run and update {len(facts['tests_to_update'])} impacted test modules.")
        if facts.get("impacted_routes"):
            recs.append("Public routes are touched: keep existing contracts backward compatible.")
        return ImpactNarrative(
            summary=(
                f"{len(facts.get('impacted_modules', []))} of {len(facts['modules'])} "
                "modules impacted, "
                f"{len(facts.get('impacted_routes', []))} routes, blast radius {score:.2f}."
            ),
            risk=risk,
            recommendations=recs,
        )

    # ------------------------------------------------------------------ planning

    def plan(self, ctx: dict[str, Any]) -> PlanDAG:
        spec = RequirementSpec.model_validate(ctx["spec"])
        return self._build_plan(spec)

    def _topic(self, spec: RequirementSpec) -> str:
        topics = self.bp.catalog["topics"]
        for cap in self.bp.catalog["order"]:
            if cap in spec.capabilities and cap in topics:
                return str(topics[cap])
        return "change"

    def _build_plan(self, spec: RequirementSpec) -> PlanDAG:
        catalog = self.bp.catalog
        topic = self._topic(spec)
        design_id = f"design.{topic}"
        defaults = catalog["defaults"]
        nodes: list[TaskNode] = [
            TaskNode(
                id=design_id,
                type=TaskType.DESIGN,
                title=f"Design: {spec.title}",
                agent="Architect",
                inputs=["spec", "impact"],
                outputs=["design"],
                write_paths=[f"docs/design/{topic}.md"],
                exit_criteria=["has_output"],
                risk=RiskLevel.LOW,
                effort_points=3,
            )
        ]
        raw_nodes = [
            dict(n) for cap in catalog["order"] if cap in spec.capabilities
            for n in catalog["capabilities"][cap].get("nodes", [])
        ]  # fmt: skip
        selected = {n["id"] for n in raw_nodes}
        for raw in raw_nodes:
            deps = [d for d in raw.get("depends_on", []) if d in selected] or [design_id]
            node_type = TaskType(raw["type"])
            default_exit = (
                defaults["test_exit"] if node_type is TaskType.TEST else defaults["implement_exit"]
            )
            nodes.append(
                TaskNode(
                    id=raw["id"],
                    type=node_type,
                    title=raw["title"],
                    agent=raw["agent"],
                    depends_on=deps,
                    inputs=[
                        "design",
                        *[i for i in raw.get("inputs", []) if self._produced(i, raw_nodes)],
                    ],
                    outputs=list(raw.get("outputs", [])),
                    write_paths=list(raw.get("write_paths", [])),
                    exit_criteria=list(raw.get("exit_criteria", default_exit)),
                    risk=RiskLevel(raw.get("risk", "low")),
                    effort_points=int(raw.get("effort_points", 1)),
                    params=dict(raw.get("params", {})),
                )
            )
        by_id = {n.id: n for n in nodes}
        children: dict[str, set[str]] = {n.id: set() for n in nodes}
        for n in nodes:
            for d in n.depends_on:
                children[d].add(n.id)

        def descendants(nid: str) -> set[str]:
            seen: set[str] = set()
            stack = [nid]
            while stack:
                for c in children[stack.pop()]:
                    if c not in seen:
                        seen.add(c)
                        stack.append(c)
            return seen

        implements = [n for n in nodes if n.type is TaskType.IMPLEMENT]
        impl_sinks = [
            n.id
            for n in implements
            if not any(by_id[d].type is TaskType.IMPLEMENT for d in descendants(n.id))
        ]
        test_writers = [
            n.id
            for n in nodes
            if n.type is TaskType.TEST and n.write_paths and n.params.get("mode") != "performance"
        ]
        untested = [
            i for i in impl_sinks if not any(by_id[d].type is TaskType.TEST for d in descendants(i))
        ]
        perf = [n.id for n in nodes if n.params.get("mode") == "performance"]
        doc_id, cov_id = f"document.{topic}", "test.coverage"
        nodes.append(
            TaskNode(
                id=doc_id,
                type=TaskType.DOCUMENT,
                title="README, API reference, OpenAPI export and CHANGELOG",
                agent="DocWriter",
                depends_on=sorted(impl_sinks) or [design_id],
                inputs=["spec", "design"],
                outputs=["docs"],
                write_paths=["README.md", "CHANGELOG.md", "docs/service/**", "docs/openapi.json"],
                exit_criteria=["has_output", "changelog_entry"],
                risk=RiskLevel.LOW,
                effort_points=2,
            )
        )
        nodes.append(
            TaskNode(
                id=cov_id,
                type=TaskType.TEST,
                title="Full service suite with coverage gate (>= 85%)",
                agent="TestEngineer",
                depends_on=sorted(set(test_writers) | set(untested) | set(impl_sinks)),
                inputs=["spec"],
                outputs=["test_report"],
                exit_criteria=["coverage:85"],
                risk=RiskLevel.LOW,
                effort_points=1,
                params={"mode": "coverage"},
            )
        )
        nodes.append(
            TaskNode(
                id="review.code",
                type=TaskType.REVIEW,
                title="Review diff against acceptance criteria and policies",
                agent="Reviewer",
                depends_on=sorted([cov_id, doc_id, *perf]),
                inputs=["spec", "test_report"],
                outputs=["review"],
                exit_criteria=["review_approved"],
                risk=RiskLevel.LOW,
                effort_points=2,
            )
        )
        nodes.append(
            TaskNode(
                id="release.readiness",
                type=TaskType.RELEASE_CHECK,
                title="Release readiness: policies, typecheck, changelog, sign-off",
                agent="ReleaseManager",
                depends_on=["review.code"],
                inputs=["spec", "review"],
                outputs=["release"],
                exit_criteria=["typecheck", "changelog_entry", "release_ready"],
                risk=spec.risk,
                effort_points=1,
            )
        )
        roots = {design_id, "implement.foundation"}
        parallel = sorted(
            n.id for n in nodes if len(n.depends_on) == 1 and n.depends_on[0] in roots
        )
        return PlanDAG(
            nodes=nodes,
            rationale=(
                f"Capabilities {spec.capabilities} decomposed from the catalog. Independent "
                f"work runs in parallel ({', '.join(parallel)}); tests and docs branch after "
                "implementation and join at review; release waits for review."
            ),
        )

    @staticmethod
    def _produced(key: str, raw_nodes: list[dict[str, Any]]) -> bool:
        return any(key in n.get("outputs", []) for n in raw_nodes)

    def delta(self, ctx: dict[str, Any]) -> DeltaPlan:
        current = [TaskNode.model_validate(n) for n in ctx["nodes"]]
        changed_nodes: list[str] = ctx.get("changed_nodes", [])
        if ctx.get("new_spec") is None or ctx.get("new_spec") == ctx.get("old_spec"):
            return DeltaPlan(
                rationale=(
                    f"Workspace files produced by {changed_nodes} changed outside the "
                    "orchestrator; their downstream validation must re-run. No structural change."
                ),
            )
        new_spec = RequirementSpec.model_validate(ctx["new_spec"])
        old_spec = RequirementSpec.model_validate(ctx["old_spec"])
        # Plan roots hang off the bootstrap "plan" node once merged into the run (engine rule).
        target = {
            n.id: (n if n.depends_on else n.model_copy(update={"depends_on": ["plan"]}))
            for n in self._build_plan(new_spec).nodes
        }
        existing = {n.id: n for n in current if n.id not in BOOTSTRAP_NODE_IDS}
        add = [n for nid, n in target.items() if nid not in existing]
        remove = [nid for nid in existing if nid not in target]
        modify = []
        for nid, node in target.items():
            if nid in existing:
                old = existing[nid]
                changes: dict[str, Any] = {}
                for attr in ("depends_on", "risk", "params", "write_paths", "exit_criteria"):
                    if getattr(old, attr) != getattr(node, attr):
                        changes[attr] = node.model_dump(mode="json")[attr]
                if changes:
                    modify.append(NodeChange(id=nid, changes=changes))
        design = next((nid for nid in existing if nid.startswith("design.")), None)
        added_caps = sorted(set(new_spec.capabilities) - set(old_spec.capabilities))
        return DeltaPlan(
            add=add,
            remove=remove,
            modify=modify,
            rerun=[design] if design and added_caps else [],
            rationale=(
                f"Requirement v{new_spec.version} adds capabilities {added_caps} "
                f"(risk {old_spec.risk.value} -> {new_spec.risk.value}); design must be revisited "
                "and "
                f"{len(add)} node(s) added, {len(modify)} re-wired."
            ),
        )

    # ------------------------------------------------------------------ design

    def design(self, ctx: dict[str, Any]) -> DesignDoc:
        spec = RequirementSpec.model_validate(ctx["spec"])
        impact = ctx.get("impact") or {}
        caps = self.bp.catalog["capabilities"]
        components, decisions, data = [], [], []
        for cap in spec.capabilities:
            notes = caps[cap].get("design")
            if not notes:
                continue
            components += notes.get("components", [])
            decisions += [
                DesignDecision(decision=d, rationale=r) for d, r in notes.get("decisions", [])
            ]
            if notes.get("data") and notes["data"] != "n/a":
                data.append(notes["data"])
        risks = [f"[{a.term}] {a.default_assumption}" for a in spec.ambiguities]
        if impact.get("migrations_needed"):
            risks.append(
                "Schema migration on a live table: additive and reversible only; gated by approval."
            )
        return DesignDoc(
            title=f"Design - {spec.title}",
            summary=(
                f"{spec.goal} Impact analysis: {impact.get('narrative', {}).get('summary', 'n/a')}"
            ),
            components=components,
            decisions=decisions,
            data_model=data,
            risks=risks,
            open_questions=list(spec.open_questions),
        )

    # ------------------------------------------------------------------ code / tests

    def change(self, ctx: dict[str, Any]) -> ChangeSet:
        node = TaskNode.model_validate(ctx["node"])
        feedback = "\n".join(ctx.get("feedback") or [])
        name = node.params.get("blueprint")
        if not name:
            raise ValueError(f"node {node.id} has no blueprint for the deterministic provider")
        main = self.bp.opset(str(name))
        chosen, note = main, ""
        if main.first_attempt:
            if not feedback:
                chosen = self.bp.opset(main.first_attempt)
                note = " (first attempt)"
            else:
                hit = next((s for s in main.repair_signatures if s in feedback), None)
                if hit is None:
                    chosen = self.bp.opset(main.first_attempt)
                    note = " (feedback not recognised; repeating previous approach)"
                else:
                    note = f" (self-repair: retry feedback shows '{hit}' failing)"
        return ChangeSet(
            summary=chosen.summary + note,
            ops=chosen.ops,
            rationale=chosen.rationale,
            alternatives=chosen.alternatives,
        )

    # ------------------------------------------------------------------ docs / review / release

    def document(self, ctx: dict[str, Any]) -> DocDraft:
        spec = RequirementSpec.model_validate(ctx["spec"])
        routes: list[dict[str, str]] = ctx.get("routes", [])
        settings: list[str] = ctx.get("settings", [])
        lines = [
            "# URL Shortener",
            "",
            "A production-minded URL shortener (FastAPI, SQLAlchemy 2.0 async, Alembic). "
            "Built and evolved by the agentic SDLC orchestrator; every change traces to a run "
            "in `runs/`.",
            "",
            "## Run",
            "",
            "```bash",
            "uvicorn service.app:create_app --factory --port 8000",
            "```",
            "",
            "## Endpoints",
            "",
            "| Method | Path | Summary |",
            "|---|---|---|",
            *[f"| {r['method']} | `{r['path']}` | {r['summary']} |" for r in routes],
            "",
            "Full reference: `docs/service/API.md`; OpenAPI: `docs/openapi.json`.",
            "",
            "## Configuration",
            "",
            "All settings are environment variables with the `SHORTENER_` prefix:",
            "",
            *[f"- `SHORTENER_{s.upper()}`" for s in settings],
            "",
            "## Requirements implemented",
            "",
            f"Latest change: **{spec.id} - {spec.title}**",
            "",
            *[f"- {f}" for f in spec.functional],
            "",
        ]
        added: list[str] = []
        fixed: list[str] = []
        for f in spec.functional:
            (fixed if f.lower().startswith("bug fix") else added).append(f"{f} ({spec.id})")
        return DocDraft(readme="\n".join(lines), changelog_added=added, changelog_fixed=fixed)

    def review(self, ctx: dict[str, Any]) -> ReviewReport:
        checks: dict[str, Any] = ctx["checks"]
        blocking: list[str] = []
        findings: list[str] = []
        for ac in checks.get("criteria", []):
            if not ac["verified"]:
                blocking.append(f"{ac['id']} not verified: {ac['reason']}")
        for p in checks.get("policy_blocks", []):
            blocking.append(f"unresolved policy block: {p}")
        for marker in checks.get("markers", []):
            findings.append(f"leftover marker: {marker}")
        findings += [f"large change: {f}" for f in checks.get("large_files", [])]
        verdict = "request_changes" if blocking else "approve"
        return ReviewReport(
            verdict=verdict,
            summary=(
                f"{sum(1 for c in checks.get('criteria', []) if c['verified'])}/"
                f"{len(checks.get('criteria', []))} acceptance criteria verified by passing tests; "
                f"{len(checks.get('changed_files', []))} files changed."
            ),
            findings=findings,
            blocking=blocking,
        )

    def release_notes(self, ctx: dict[str, Any]) -> ReleaseNotes:
        spec = RequirementSpec.model_validate(ctx["spec"])
        return ReleaseNotes(
            version=ctx.get("version", "unreleased"),
            highlights=list(spec.functional)[:6],
            notes=f"{spec.id}: {spec.goal}",
        )
