"""Reviewer and ReleaseManager: read/run-only agents that judge, never write."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.agents.base import READ, RUN, Agent, AgentContext
from orchestrator.agents.contracts import ReleaseNotes, ReviewReport
from orchestrator.models import AgentResult

REVIEW_SYSTEM = """You are the Reviewer. Judge the change against the acceptance criteria and the
evidence provided (which criteria are verified by passing tests, leftover markers, large files).
Approve only if every criterion is verified and nothing blocking remains. Be specific."""

RELEASE_SYSTEM = """You are the ReleaseManager. Write concise release notes for the change."""

_MARKERS = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b|\bprint\(|breakpoint\(\)")


async def _run_tests(ctx: AgentContext, test_ids: list[str]) -> dict[str, str]:
    """Run the given pytest node ids; return {node_id: outcome}."""
    if not test_ids:
        return {}
    tools = ctx.require_tools()
    # A missing test id makes pytest abort collection for *all* ids, so check existence first.
    outcomes: dict[str, str] = {}
    runnable: list[str] = []
    for tid in test_ids:
        path, _, name = tid.partition("::")
        if tools.exists(path) and re.search(rf"def {re.escape(name)}\b", tools.read(path)):
            runnable.append(tid)
        else:
            outcomes[tid] = "not found"
    if not runnable:
        return outcomes
    # Parse pytest's own "-rA" short summary ("PASSED path::name") instead of JUnit XML: the
    # output comes from running agent-written code, so it is treated as untrusted text.
    res = await tools.run_python(
        "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", "--tb=no", "--show-capture=no",
        *runnable, timeout_s=600,
    )  # fmt: skip
    summary = {"PASSED": "passed", "FAILED": "failed", "ERROR": "failed", "SKIPPED": "skipped"}
    for line in res.stdout.splitlines():
        word, _, rest = line.partition(" ")
        node_id = rest.split(" - ")[0].strip()
        if word in summary and node_id in runnable:
            outcomes[node_id] = summary[word]
    for tid in runnable:
        outcomes.setdefault(tid, "error")
    return outcomes


class Reviewer(Agent):
    name = "Reviewer"
    permissions = frozenset({READ, RUN})
    description = (
        "Checks the diff against acceptance criteria (by running the proving tests) and hygiene."
    )

    async def run(self, ctx: AgentContext) -> AgentResult:
        tools = ctx.require_tools()
        spec = ctx.spec
        if spec is None:
            raise RuntimeError("Reviewer needs the spec artifact")
        test_ids = sorted(
            {v for ac in spec.acceptance_criteria for v in ac.verified_by if "::" in v}
        )
        outcomes = await _run_tests(ctx, test_ids)
        gate_artifacts = {
            a.key: a.content for a in ctx.context.store.artifacts() if a.key.startswith("gates:")
        }
        passed_gates = {
            g["gate"].split(":")[0]
            for results in gate_artifacts.values()
            for g in results
            if g.get("passed")
        }
        criteria = []
        for ac in spec.acceptance_criteria:
            missing = []
            for ref in ac.verified_by:
                if ref.startswith("gate:"):
                    if ref[5:] not in passed_gates:
                        missing.append(f"{ref} not passed")
                elif outcomes.get(ref) != "passed":
                    missing.append(f"{ref} -> {outcomes.get(ref, 'not found')}")
            if not ac.verified_by:
                missing.append("no verifying test declared")
            criteria.append({"id": ac.id, "verified": not missing, "reason": "; ".join(missing)})

        baseline = (ctx.context.content("baseline") or {}).get("commit")
        diff_res = await tools.run(["git", "diff", "--numstat", baseline or "HEAD"], timeout_s=60)
        changed, large = [], []
        for line in diff_res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                changed.append(parts[2])
                if parts[0].isdigit() and int(parts[0]) > 400:
                    large.append(f"{parts[2]} (+{parts[0]})")
        markers = []
        for rel in [c for c in changed if c.startswith("service/") and c.endswith(".py")]:
            if tools.exists(rel):
                for n, line in enumerate(tools.read(rel).splitlines(), 1):
                    if _MARKERS.search(line):
                        markers.append(f"{rel}:{n}")
        checks: dict[str, Any] = {
            "criteria": criteria,
            "changed_files": changed,
            "large_files": large,
            "markers": markers,
            "policy_blocks": [],
        }
        report = await ctx.require_llm().structured(
            role=self.name,
            task="review",
            system=REVIEW_SYSTEM,
            prompt=f"Spec:\n{spec.model_dump_json()}\nEvidence:\n{json.dumps(checks)[:30000]}",
            schema=ReviewReport,
            context={"spec": spec.model_dump(mode="json"), "checks": checks},
        )
        # Guardrail: the model cannot approve over unverified acceptance criteria.
        unverified = [c for c in criteria if not c["verified"]]
        if unverified and report.verdict == "approve":
            report.verdict = "request_changes"
            report.blocking += [f"{c['id']} unverified: {c['reason']}" for c in unverified]
        return AgentResult(
            summary=f"review {report.verdict}: {report.summary}",
            artifacts={
                "review": {
                    **report.model_dump(mode="json"),
                    "criteria": criteria,
                    "tests": outcomes,
                }
            },
            rationale=report.summary,
            alternatives=[
                "Approve on LLM judgement alone (rejected: approvals require test evidence)"
            ],
        )


class ReleaseManager(Agent):
    name = "ReleaseManager"
    permissions = frozenset({READ, RUN})
    description = "Release readiness checklist and notes; the human sign-off gate follows."

    async def run(self, ctx: AgentContext) -> AgentResult:
        tools = ctx.require_tools()
        spec = ctx.spec
        if spec is None:
            raise RuntimeError("ReleaseManager needs the spec artifact")
        review = ctx.context.content("review", {})
        coverage = ctx.context.content("gates:test.coverage", [])
        checklist: list[dict[str, Any]] = []

        def item(name: str, passed: bool, detail: str) -> None:
            checklist.append({"name": name, "passed": passed, "detail": detail})

        criteria = review.get("criteria", [])
        item(
            "acceptance_criteria_verified",
            bool(criteria) and all(c["verified"] for c in criteria),
            f"{sum(c['verified'] for c in criteria)}/{len(criteria)} verified",
        )
        item("review_approved", review.get("verdict") == "approve", str(review.get("summary", "")))
        cov = next((g for g in coverage if g["gate"].startswith("coverage")), None)
        item(
            "coverage_gate_passed",
            bool(cov and cov["passed"]),
            cov["details"] if cov else "missing",
        )
        changelog = tools.read("CHANGELOG.md") if tools.exists("CHANGELOG.md") else ""
        unreleased = changelog.partition("## [Unreleased]")[2].partition("\n## [")[0]
        item("changelog_entry", "\n- " in unreleased, "Unreleased section has entries")
        if tools.exists("service/alembic.ini"):
            with tempfile.TemporaryDirectory() as tmp:
                url = f"sqlite+aiosqlite:///{Path(tmp) / 'r.db'}"
                ok, detail = True, "upgrade head / downgrade base / upgrade head"
                for step in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
                    res = await tools.run(
                        [tools.python, "-m", "alembic", "-c", "service/alembic.ini", *step],
                        timeout_s=180,
                        env={"SHORTENER_DATABASE_URL": url},
                    )
                    if not res.ok:
                        ok, detail = False, res.tail(5)
                        break
            item("migrations_round_trip", ok, detail)
        notes = await ctx.require_llm().structured(
            role=self.name,
            task="release_notes",
            system=RELEASE_SYSTEM,
            prompt=f"Spec:\n{spec.model_dump_json()}\nChecklist:\n{json.dumps(checklist)}",
            schema=ReleaseNotes,
            context={"spec": spec.model_dump(mode="json"), "checklist": checklist},
        )
        ready = all(c["passed"] for c in checklist)
        passed = sum(1 for c in checklist if c["passed"])
        return AgentResult(
            summary=f"release {'READY' if ready else 'NOT ready'}: {passed}/{len(checklist)}",
            artifacts={
                "release": {
                    "ready": ready,
                    "checklist": checklist,
                    "notes": notes.model_dump(mode="json"),
                }
            },
            rationale=notes.notes,
        )
