"""Built-in gates against a tiny real workspace (ruff/pytest/mypy run as subprocesses)."""

import sys
from pathlib import Path

import pytest

from orchestrator.context import ContextStore
from orchestrator.gates import GateContext, registry
from orchestrator.models import AgentResult
from orchestrator.state import StateStore
from orchestrator.workspace import AppliedChange, Workspace
from tests.orchestrator.fakes import node

PYPROJECT = """
[tool.pytest.ini_options]
asyncio_mode = "auto"
[tool.mypy]
strict = true
"""


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    (root / "service").mkdir(parents=True)
    (root / "tests" / "service").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "service" / "__init__.py").write_text("")
    (root / "service" / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "service" / "__init__.py").write_text("")
    (root / "tests" / "service" / "test_calc.py").write_text(
        "from service.calc import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
    )
    return Workspace(root, python=sys.executable)


def gctx(ws: Workspace, tmp_path: Path, **kw: object) -> GateContext:
    store = StateStore(tmp_path / "state.db")
    store.create_run("r", scenario=None, requirement_path="r", workspace=str(ws.root), config={})
    params = kw.pop("params", {})
    n = node("n", params=params)  # type: ignore[arg-type]
    return GateContext(node=n, attempt=1, context=ContextStore(store), workspace=ws, **kw)  # type: ignore[arg-type]


def changed(*files: str) -> AppliedChange:
    return AppliedChange(files=list(files))


async def test_compiles_and_lint(ws: Workspace, tmp_path: Path) -> None:
    ok = await registry.evaluate("compiles", gctx(ws, tmp_path, applied=changed("service/calc.py")))
    assert ok.passed
    (ws.root / "service" / "bad.py").write_text("def f(:\n")
    bad = await registry.evaluate(
        "compiles", gctx(ws, tmp_path / "b", applied=changed("service/bad.py"))
    )
    assert not bad.passed
    lint = await registry.evaluate(
        "lint_clean", gctx(ws, tmp_path / "c", applied=changed("service/calc.py"))
    )
    assert lint.passed, lint.details
    (ws.root / "service" / "unused.py").write_text("import os\n")
    dirty = await registry.evaluate(
        "lint_clean", gctx(ws, tmp_path / "d", applied=changed("service/unused.py"))
    )
    assert not dirty.passed
    assert "F401" in dirty.details
    none = await registry.evaluate("lint_clean", gctx(ws, tmp_path / "e", applied=changed("x.md")))
    assert none.passed


async def test_targeted_tests_pass_and_fail(ws: Workspace, tmp_path: Path) -> None:
    good = await registry.evaluate(
        "targeted_tests", gctx(ws, tmp_path, params={"tests": ["tests/service/test_calc.py"]})
    )
    assert good.passed
    assert "1 passed" in good.details
    missing = await registry.evaluate(
        "targeted_tests", gctx(ws, tmp_path / "m", params={"tests": []})
    )
    assert not missing.passed
    (ws.root / "service" / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n"
    )
    bad = await registry.evaluate(
        "targeted_tests", gctx(ws, tmp_path / "b", params={"tests": ["tests/service/test_calc.py"]})
    )
    assert not bad.passed
    assert "assert -1 == 3" in bad.details


async def test_tests_fail_first_requires_red(ws: Workspace, tmp_path: Path) -> None:
    params = {"tests": ["tests/service/test_calc.py"]}
    green = await registry.evaluate("tests_fail_first", gctx(ws, tmp_path, params=params))
    assert not green.passed
    assert "does not reproduce" in green.details
    (ws.root / "service" / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return 0\n")
    red = await registry.evaluate("tests_fail_first", gctx(ws, tmp_path / "r", params=params))
    assert red.passed
    (ws.root / "tests" / "service" / "test_calc.py").write_text("import nonexistent_module\n")
    err = await registry.evaluate("tests_fail_first", gctx(ws, tmp_path / "e", params=params))
    assert not err.passed
    assert "errored" in err.details
    nothing = await registry.evaluate("tests_fail_first", gctx(ws, tmp_path / "n"))
    assert not nothing.passed


async def test_coverage_and_typecheck(ws: Workspace, tmp_path: Path) -> None:
    cov = await registry.evaluate("coverage:85", gctx(ws, tmp_path))
    assert cov.passed, cov.details
    assert "100" in cov.details
    strict = await registry.evaluate("coverage:101", gctx(ws, tmp_path / "s"))
    assert not strict.passed
    typed = await registry.evaluate("typecheck", gctx(ws, tmp_path / "t"))
    assert typed.passed, typed.details
    (ws.root / "service" / "untyped.py").write_text("def f(x):\n    return x\n")
    untyped = await registry.evaluate("typecheck", gctx(ws, tmp_path / "u"))
    assert not untyped.passed


async def test_result_based_gates(ws: Workspace, tmp_path: Path) -> None:
    def res(**artifacts: object) -> AgentResult:
        return AgentResult(summary="s", artifacts=artifacts)

    approve = res(review={"verdict": "approve", "findings": []})
    reject = res(review={"verdict": "request_changes", "blocking": ["x"]})
    assert (await registry.evaluate("review_approved", gctx(ws, tmp_path, result=approve))).passed
    assert not (
        await registry.evaluate("review_approved", gctx(ws, tmp_path / "a", result=reject))
    ).passed

    ready = res(release={"ready": True, "checklist": [{"name": "a", "passed": True}]})
    not_ready = res(release={"ready": False, "checklist": [{"name": "a", "passed": False}]})
    assert (await registry.evaluate("release_ready", gctx(ws, tmp_path / "b", result=ready))).passed
    assert not (
        await registry.evaluate("release_ready", gctx(ws, tmp_path / "c", result=not_ready))
    ).passed

    fast = res(performance={"p95_ms": 4.0, "target_p95_ms": 50, "p50_ms": 2.0})
    slow = res(performance={"p95_ms": 80.0, "target_p95_ms": 50})
    assert (
        await registry.evaluate("p95_under_target", gctx(ws, tmp_path / "d", result=fast))
    ).passed
    assert not (
        await registry.evaluate("p95_under_target", gctx(ws, tmp_path / "e", result=slow))
    ).passed
    assert not (
        await registry.evaluate("p95_under_target", gctx(ws, tmp_path / "f", result=res()))
    ).passed

    outputs = gctx(ws, tmp_path / "g", result=res(a=1))
    outputs.node = node("n", outputs=["a", "b"])
    out = await registry.evaluate("has_output", outputs)
    assert not out.passed
    assert "'b'" in out.details


async def test_changelog_and_unknown_and_crashing_gates(ws: Workspace, tmp_path: Path) -> None:
    assert not (await registry.evaluate("changelog_entry", gctx(ws, tmp_path))).passed
    (ws.root / "CHANGELOG.md").write_text("## [Unreleased]\n### Added\n- thing\n")
    assert (await registry.evaluate("changelog_entry", gctx(ws, tmp_path / "a"))).passed
    unknown = await registry.evaluate("no_such_gate", gctx(ws, tmp_path / "b"))
    assert not unknown.passed
    no_ws = gctx(ws, tmp_path / "c")
    no_ws.workspace = None
    crashed = await registry.evaluate("compiles", no_ws)
    assert crashed.passed  # no python files changed -> no workspace needed
    crashed2 = await registry.evaluate("typecheck", no_ws)
    assert not crashed2.passed
    assert "gate error" in crashed2.details
