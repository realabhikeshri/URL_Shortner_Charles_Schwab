"""Entry/exit gates.

A node's ``entry_criteria`` / ``exit_criteria`` are gate names, optionally with an argument
(``coverage:85``). Gates are small async checks that return a :class:`GateResult`; failing
exit gates trigger rollback + retry, and their output becomes the agent's repair feedback.
"""

from __future__ import annotations

import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.context import ContextStore
from orchestrator.models import AgentResult, GateResult, TaskNode
from orchestrator.workspace import AppliedChange, Workspace


@dataclass
class GateContext:
    node: TaskNode
    attempt: int
    context: ContextStore
    workspace: Workspace | None = None
    result: AgentResult | None = None
    applied: AppliedChange | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    def require_workspace(self) -> Workspace:
        if self.workspace is None:
            raise RuntimeError(f"gate on {self.node.id} needs a workspace")
        return self.workspace

    def changed_python(self) -> list[str]:
        if self.applied is None:
            return []
        deleted = set(self.applied.deleted)
        return [f for f in self.applied.files if f.endswith(".py") and f not in deleted]


GateFn = Callable[[GateContext, str | None], Awaitable[GateResult]]


class GateRegistry:
    def __init__(self) -> None:
        self._gates: dict[str, GateFn] = {}

    def register(self, name: str) -> Callable[[GateFn], GateFn]:
        def deco(fn: GateFn) -> GateFn:
            self._gates[name] = fn
            return fn

        return deco

    def add(self, name: str, fn: GateFn) -> None:
        self._gates[name] = fn

    def names(self) -> list[str]:
        return sorted(self._gates)

    async def evaluate(self, spec: str, ctx: GateContext) -> GateResult:
        name, _, arg = spec.partition(":")
        fn = self._gates.get(name)
        started = time.perf_counter()
        if fn is None:
            return GateResult(gate=spec, passed=False, details=f"unknown gate '{name}'")
        try:
            result = await fn(ctx, arg or None)
        except Exception as exc:  # a crashing gate is a failing gate, never a crashing run
            result = GateResult(gate=spec, passed=False, details=f"gate error: {exc!r}")
        result.gate = spec
        result.duration_s = round(time.perf_counter() - started, 3)
        return result


registry = GateRegistry()


def _ok(details: str = "") -> GateResult:
    return GateResult(gate="", passed=True, details=details)


def _fail(details: str) -> GateResult:
    return GateResult(gate="", passed=False, details=details)


@registry.register("inputs_available")
async def inputs_available(ctx: GateContext, _: str | None) -> GateResult:
    missing = [k for k in ctx.node.inputs if not ctx.context.has(k)]
    return _fail(f"missing input artifacts: {missing}") if missing else _ok("all inputs present")


@registry.register("has_output")
async def has_output(ctx: GateContext, _: str | None) -> GateResult:
    produced = set(ctx.result.artifacts) if ctx.result else set()
    missing = [k for k in ctx.node.outputs if k not in produced]
    return _fail(f"declared outputs not produced: {missing}") if missing else _ok()


@registry.register("compiles")
async def compiles(ctx: GateContext, _: str | None) -> GateResult:
    files = ctx.changed_python()
    if not files:
        return _ok("no python files changed")
    res = await ctx.require_workspace().run_python("-m", "py_compile", *files, timeout_s=120)
    return _ok(f"{len(files)} files compile") if res.ok else _fail(res.tail())


@registry.register("lint_clean")
async def lint_clean(ctx: GateContext, _: str | None) -> GateResult:
    files = ctx.changed_python()
    if not files:
        return _ok("no python files changed")
    ws = ctx.require_workspace()
    res = await ws.run_python("-m", "ruff", "check", "--no-cache", *files, timeout_s=120)
    if not res.ok:
        return _fail(res.tail())
    fmt = await ws.run_python(
        "-m", "ruff", "format", "--check", "--no-cache", *files, timeout_s=120
    )
    return _ok(f"ruff clean on {len(files)} files") if fmt.ok else _fail(fmt.tail())


def _node_tests(ctx: GateContext) -> list[str]:
    tests = ctx.node.params.get("tests") or []
    return [str(t) for t in tests]


_SUMMARY = re.compile(r"=+ (.*?(?:passed|failed|error).*?) in [\d.]+s")


def _pytest_summary(output: str) -> str:
    found = _SUMMARY.findall(output)
    return found[-1] if found else output.strip().splitlines()[-1] if output.strip() else ""


@registry.register("targeted_tests")
async def targeted_tests(ctx: GateContext, _: str | None) -> GateResult:
    ws = ctx.require_workspace()
    tests = [t for t in _node_tests(ctx) if ws.exists(t.split("::")[0])]
    if not tests:
        return _fail("no targeted tests exist for this node (a change must be covered by tests)")
    res = await ws.run_python("-m", "pytest", "-q", "-p", "no:cacheprovider", *tests, timeout_s=600)
    summary = _pytest_summary(res.stdout)
    return _ok(f"{summary} [{', '.join(tests)}]") if res.ok else _fail(res.tail(60))


@registry.register("tests_fail_first")
async def tests_fail_first(ctx: GateContext, _: str | None) -> GateResult:
    """Red phase of red/green: the new regression test must fail against current code."""
    ws = ctx.require_workspace()
    tests = _node_tests(ctx)
    if not tests:
        return _fail("no regression tests declared")
    res = await ws.run_python("-m", "pytest", "-q", "-p", "no:cacheprovider", *tests, timeout_s=600)
    if res.returncode == 1:
        return _ok(
            f"regression test fails as expected before the fix: {_pytest_summary(res.stdout)}"
        )
    if res.ok:
        return _fail("regression test passes before the fix; it does not reproduce the bug")
    return _fail(
        f"regression test errored instead of failing (exit {res.returncode}):\n{res.tail()}"
    )


_TOTAL = re.compile(r"^TOTAL\s+.*?(\d+(?:\.\d+)?)%\s*$", re.MULTILINE)


@registry.register("coverage")
async def coverage(ctx: GateContext, arg: str | None) -> GateResult:
    threshold = float(arg or 85)
    ws = ctx.require_workspace()
    res = await ws.run_python(
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--cov=service",
        "--cov-report=term",
        "tests/service",
        timeout_s=900,
    )
    match = _TOTAL.search(res.stdout)
    pct = float(match.group(1)) if match else 0.0
    summary = _pytest_summary(res.stdout)
    if not res.ok:
        return _fail(f"test suite failed ({summary}):\n{res.tail(60)}")
    if pct < threshold:
        return _fail(f"coverage {pct:.1f}% < {threshold:.0f}% ({summary})")
    return _ok(f"coverage {pct:.1f}% >= {threshold:.0f}% ({summary})")


@registry.register("typecheck")
async def typecheck(ctx: GateContext, _: str | None) -> GateResult:
    ws = ctx.require_workspace()
    res = await ws.run_python("-m", "mypy", "service", "--no-incremental", timeout_s=600)
    return _ok(res.stdout.strip().splitlines()[-1]) if res.ok else _fail(res.tail(40))


@registry.register("migrations_apply")
async def migrations_apply(ctx: GateContext, _: str | None) -> GateResult:
    """upgrade head -> downgrade base -> upgrade head on a throwaway SQLite database."""
    ws = ctx.require_workspace()
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{Path(tmp) / 'migrations.db'}"
        steps = (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"])
        for step in steps:
            res = await ws.run(
                [ws.python, "-m", "alembic", "-c", "service/alembic.ini", *step],
                timeout_s=180,
                env={"SHORTENER_DATABASE_URL": url},
            )
            if not res.ok:
                return _fail(f"alembic {' '.join(step)} failed:\n{res.tail()}")
    return _ok("upgrade/downgrade/upgrade round-trip succeeded")


@registry.register("review_approved")
async def review_approved(ctx: GateContext, _: str | None) -> GateResult:
    review = (ctx.result.artifacts.get("review") if ctx.result else None) or {}
    verdict = review.get("verdict")
    if verdict == "approve":
        return _ok(f"reviewer approved ({len(review.get('findings', []))} non-blocking findings)")
    return _fail(f"reviewer verdict={verdict}: {review.get('blocking', [])}")


@registry.register("release_ready")
async def release_ready(ctx: GateContext, _: str | None) -> GateResult:
    release = (ctx.result.artifacts.get("release") if ctx.result else None) or {}
    failed = [c["name"] for c in release.get("checklist", []) if not c.get("passed")]
    if release.get("ready") and not failed:
        return _ok(f"{len(release.get('checklist', []))} release checks passed")
    return _fail(f"release checklist failures: {failed}")


@registry.register("changelog_entry")
async def changelog_entry(ctx: GateContext, _: str | None) -> GateResult:
    ws = ctx.require_workspace()
    if not ws.exists("CHANGELOG.md"):
        return _fail("CHANGELOG.md missing")
    text = ws.read("CHANGELOG.md")
    match = re.search(r"## \[Unreleased\](.*?)(?:\n## \[|\Z)", text, re.DOTALL)
    if match and re.search(r"^- ", match.group(1), re.MULTILINE):
        return _ok("CHANGELOG has Unreleased entries")
    return _fail("CHANGELOG.md has no entries under ## [Unreleased]")


@registry.register("p95_under_target")
async def p95_under_target(ctx: GateContext, _: str | None) -> GateResult:
    perf = (ctx.result.artifacts.get("performance") if ctx.result else None) or {}
    p95, target = perf.get("p95_ms"), perf.get("target_p95_ms")
    if p95 is None or target is None:
        return _fail("no performance measurement produced")
    if p95 <= target:
        return _ok(f"p95 {p95:.1f}ms <= target {target}ms (p50 {perf.get('p50_ms', 0):.1f}ms)")
    return _fail(f"p95 {p95:.1f}ms exceeds target {target}ms")
