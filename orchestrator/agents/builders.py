"""Implementer, TestEngineer and DocWriter: agents that propose change sets."""

from __future__ import annotations

import json
import re
from typing import Any

from orchestrator.agents.base import READ, RUN, WRITE, Agent, AgentContext
from orchestrator.agents.contracts import DocDraft
from orchestrator.analysis.codebase import INTROSPECT
from orchestrator.models import AgentResult, ChangeSet, FileOp

IMPLEMENTER_SYSTEM = """You are the Implementer. Produce a ChangeSet (write/replace/delete ops) that
implements exactly the task described, inside the task's write_paths only, consistent with the
design, typed for mypy --strict, ruff-clean, with focused unit tests. If retry feedback is given
(test failures, lint errors, policy violations), fix the root cause it points to."""

TEST_SYSTEM = """You are the TestEngineer. Produce a ChangeSet adding tests that prove the
acceptance criteria for this task: integration tests through the HTTP API, property-based tests
where inputs are open-ended, and regression tests that fail before a fix. Tests must be
deterministic."""

DOC_SYSTEM = """You are the DocWriter. Given the spec, the real route table and the settings, write
the service README and the CHANGELOG entries (Keep a Changelog categories) for this change."""


def _existing_files(ctx: AgentContext) -> dict[str, str]:
    tools = ctx.require_tools()
    files: dict[str, str] = {}
    for pattern in ctx.node.write_paths:
        if any(ch in pattern for ch in "*?["):
            for rel in tools.list_files(pattern):
                files[rel] = tools.read(rel)
        elif tools.exists(pattern):
            files[pattern] = tools.read(pattern)
    return files


async def _propose(agent: Agent, ctx: AgentContext, task: str, system: str) -> ChangeSet:
    files = _existing_files(ctx)
    design = ctx.context.content("design", {})
    prompt = (
        f"Task {ctx.node.id}: {ctx.node.title}\nwrite_paths: {ctx.node.write_paths}\n"
        f"Design decisions: {json.dumps(design.get('decisions', []))[:4000]}\n"
        f"Current files:\n"
        + "\n".join(f"--- {p}\n{c}" for p, c in files.items())[:60000]
        + ("\nRetry feedback:\n" + "\n".join(ctx.feedback)[-6000:] if ctx.feedback else "")
    )
    return await ctx.require_llm().structured(
        role=agent.name,
        task=task,
        system=system,
        prompt=prompt,
        schema=ChangeSet,
        context={
            "node": ctx.node.model_dump(mode="json"),
            "feedback": ctx.feedback,
            "attempt": ctx.attempt,
        },
    )


class Implementer(Agent):
    name = "Implementer"
    permissions = frozenset({READ, WRITE, RUN})
    description = "Writes production code (plus focused unit tests) for one plan node."

    async def run(self, ctx: AgentContext) -> AgentResult:
        cs = await _propose(self, ctx, "implement", IMPLEMENTER_SYSTEM)
        files = sorted({op.path for op in cs.ops})
        return AgentResult(
            summary=cs.summary,
            artifacts={key: {"files": files} for key in ctx.node.outputs},
            changeset=cs,
            rationale=cs.rationale,
            alternatives=cs.alternatives,
        )


class TestEngineer(Agent):
    __test__ = False  # not a pytest test class
    name = "TestEngineer"
    permissions = frozenset({READ, WRITE, RUN})
    description = "Writes test suites; measures coverage inventory and performance."

    async def run(self, ctx: AgentContext) -> AgentResult:
        mode = ctx.node.params.get("mode")
        if mode == "coverage":
            return await self._inventory(ctx)
        if mode == "performance":
            return await self._performance(ctx)
        cs = await _propose(self, ctx, "write_tests", TEST_SYSTEM)
        files = sorted({op.path for op in cs.ops})
        return AgentResult(
            summary=cs.summary,
            artifacts={key: {"files": files} for key in ctx.node.outputs},
            changeset=cs,
            rationale=cs.rationale,
            alternatives=cs.alternatives,
        )

    async def _inventory(self, ctx: AgentContext) -> AgentResult:
        tools = ctx.require_tools()
        res = await tools.run_python(
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/service",
            timeout_s=300,
        )
        per_file: dict[str, int] = {}
        for line in res.stdout.splitlines():
            if "::" in line:
                path = line.split("::")[0]
                per_file[path] = per_file.get(path, 0) + 1
        total = sum(per_file.values())
        return AgentResult(
            summary=f"{total} tests collected across {len(per_file)} modules",
            artifacts={"test_report": {"collected": total, "per_file": per_file}},
            rationale="Coverage is measured by the independent coverage gate, not self-reported.",
        )

    async def _performance(self, ctx: AgentContext) -> AgentResult:
        tools = ctx.require_tools()
        p = ctx.node.params
        res = await tools.run_python(
            "scripts/loadcheck.py",
            "--requests", str(p.get("requests", 600)),
            "--concurrency", str(p.get("concurrency", 20)),
            "--target-p95-ms", str(p.get("target_p95_ms", 50)),
            "--json",
            timeout_s=300,
        )  # fmt: skip
        if not res.ok:
            raise RuntimeError(f"loadcheck failed: {res.tail(20)}")
        data: dict[str, Any] = json.loads(res.stdout.strip().splitlines()[-1])
        render = await tools.run_python(
            "scripts/loadcheck.py", "--render", json.dumps(data), timeout_s=60
        )
        current = tools.read("docs/PERFORMANCE.md") if tools.exists("docs/PERFORMANCE.md") else ""
        content = _replace_block(current, render.stdout, "loadcheck")
        return AgentResult(
            summary=f"redirect p50 {data['p50_ms']:.1f} ms, p95 {data['p95_ms']:.1f} ms "
            f"(target {data['target_p95_ms']} ms)",
            artifacts={"performance": data},
            changeset=ChangeSet(
                summary="record measured redirect latency",
                ops=[FileOp(op="write", path="docs/PERFORMANCE.md", content=content)],
            ),
            rationale="Measured against a real uvicorn process over HTTP (not in-process).",
        )


def _replace_block(text: str, block: str, name: str) -> str:
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    body = f"{start}\n{block.strip()}\n{end}"
    if start in text and end in text:
        return re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: body, text, flags=re.S)
    header = text if text.strip() else "# Performance\n\n"
    return header.rstrip() + "\n\n" + body + "\n"


CATEGORIES = ("Added", "Changed", "Fixed", "Security")


def merge_changelog(text: str, entries: dict[str, list[str]]) -> str:
    """Insert entries under ``## [Unreleased]`` (Keep a Changelog), de-duplicating bullets."""
    if "## [Unreleased]" not in text:
        text = "# Changelog\n\n## [Unreleased]\n" + text
    head, _, rest = text.partition("## [Unreleased]")
    section, sep, tail = rest.partition("\n## [")
    existing: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    current: str | None = None
    for line in section.splitlines():
        if line.startswith("### "):
            current = line[4:].strip()
            existing.setdefault(current, [])
        elif line.startswith("- ") and current:
            existing[current].append(line[2:])
    for cat, items in entries.items():
        for item in items:
            if item not in existing.setdefault(cat, []):
                existing[cat].append(item)
    out = [head + "## [Unreleased]"]
    for cat, items in existing.items():
        if items:
            out += ["", f"### {cat}", *[f"- {i}" for i in items]]
    body = "\n".join(out) + "\n"
    return body + (("\n## [" + tail) if sep else "")


def render_api_doc(openapi: dict[str, Any]) -> str:
    lines = [
        f"# {openapi.get('info', {}).get('title', 'API')} reference",
        "",
        f"Version {openapi.get('info', {}).get('version', '?')}. Generated from the live OpenAPI "
        "schema by the DocWriter agent; do not edit by hand.",
        "",
    ]
    for path, ops in sorted(openapi.get("paths", {}).items()):
        for method, op in sorted(ops.items()):
            lines += [f"## `{method.upper()} {path}`", "", op.get("summary", ""), ""]
            params = op.get("parameters", [])
            if params:
                lines += ["| Parameter | In | Required |", "|---|---|---|"]
                lines += [
                    f"| `{p['name']}` | {p['in']} | {p.get('required', False)} |" for p in params
                ]
                lines.append("")
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            if body:
                ref = body.get("schema", {}).get("$ref", "").rsplit("/", 1)[-1]
                lines += [f"Request body: `{ref or 'json'}`", ""]
            codes = ", ".join(f"`{c}`" for c in sorted(op.get("responses", {})))
            lines += [f"Responses: {codes}", ""]
    return "\n".join(lines)


class DocWriter(Agent):
    name = "DocWriter"
    permissions = frozenset({READ, WRITE, RUN})
    description = "README, API reference from the live OpenAPI schema, OpenAPI export, CHANGELOG."

    async def run(self, ctx: AgentContext) -> AgentResult:
        tools = ctx.require_tools()
        spec = ctx.spec
        if spec is None:
            raise RuntimeError("DocWriter needs the spec artifact")
        res = await tools.run_python("-c", INTROSPECT, "--openapi", timeout_s=120)
        if not res.ok:
            raise RuntimeError(f"could not introspect the app: {res.tail(10)}")
        data = json.loads(res.stdout.strip().splitlines()[-1])
        openapi = data.get("openapi") or {}
        routes = [r for r in data.get("routes", []) if r["method"] != "HEAD"]
        draft = await ctx.require_llm().structured(
            role=self.name,
            task="document",
            system=DOC_SYSTEM,
            prompt=(
                f"Spec:\n{spec.model_dump_json()}\nRoutes:\n{json.dumps(routes)}\n"
                f"Settings: {data.get('settings')}"
            ),
            schema=DocDraft,
            context={
                "spec": spec.model_dump(mode="json"),
                "routes": routes,
                "settings": data.get("settings", []),
            },
        )
        changelog = tools.read("CHANGELOG.md") if tools.exists("CHANGELOG.md") else ""
        merged = merge_changelog(
            changelog,
            {
                "Added": draft.changelog_added,
                "Changed": draft.changelog_changed,
                "Fixed": draft.changelog_fixed,
                "Security": draft.changelog_security,
            },
        )
        ops = [
            FileOp(op="write", path="README.md", content=draft.readme),
            FileOp(op="write", path="CHANGELOG.md", content=merged),
            FileOp(op="write", path="docs/service/API.md", content=render_api_doc(openapi)),
            FileOp(
                op="write",
                path="docs/openapi.json",
                content=json.dumps(openapi, indent=2, sort_keys=True) + "\n",
            ),
        ]
        n_entries = len(draft.changelog_added) + len(draft.changelog_fixed)
        return AgentResult(
            summary=f"docs for {len(routes)} routes; {n_entries} changelog entries",
            artifacts={"docs": {"files": [o.path for o in ops], "routes": len(routes)}},
            changeset=ChangeSet(summary="documentation", ops=ops),
            rationale=(
                "API reference and OpenAPI are generated from the running app, so they cannot "
                "drift from the code."
            ),
        )
