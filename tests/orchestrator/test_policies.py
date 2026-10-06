import sys
from pathlib import Path

import pytest

from orchestrator.models import ChangeSet, FileOp, PolicyOutcome, TaskType
from orchestrator.policy.checks import shannon_entropy
from orchestrator.policy.engine import PolicyEngine, PolicyInput, PolicyRule
from orchestrator.workspace import AppliedChange, Workspace
from tests.orchestrator.fakes import node

ROOT = Path(__file__).resolve().parents[2]
ENGINE = PolicyEngine.from_dir(ROOT / "policies")


def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    (root / "service").mkdir(parents=True)
    (root / "service" / "config.py").write_text("class Settings:\n    debug: bool = False\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0]\n- old\n")
    return Workspace(root, python=sys.executable)


async def evaluate(
    ws: Workspace,
    files: dict[str, str],
    *,
    stage: str = "node",
    type_: TaskType = TaskType.IMPLEMENT,
) -> dict[str, PolicyOutcome]:
    applied = ws.apply(
        ChangeSet(summary="t", ops=[FileOp(op="write", path=p, content=c) for p, c in files.items()])
    ) if files else AppliedChange()  # fmt: skip
    results = await ENGINE.evaluate(
        PolicyInput(node=node("n", type_=type_), applied=applied, workspace=ws, stage=stage)
    )
    return {r.policy_id: r.outcome for r in results}


def failing(outcomes: dict[str, PolicyOutcome]) -> set[str]:
    return {k for k, v in outcomes.items() if v is not PolicyOutcome.PASS}


async def test_clean_change_passes_every_node_policy(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    out = await evaluate(
        ws, {"service/util.py": "def add(a: int, b: int) -> int:\n    return a + b\n"}
    )
    assert failing(out) == set()
    assert "CHG-004-changelog" not in out  # release-only policy


@pytest.mark.parametrize(
    "line",
    [
        'AWS = "AKIAABCDEFGHIJKLMNOP"',
        'api_key = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"',
        'password = "hunter2hunter2"',
        'BLOB = "q8Zr1Lp0Xk7Vb2Nm4Wc9Ty6Hs3Jd5Fg"',
    ],
)
async def test_secret_scan_blocks_secrets(tmp_path: Path, line: str) -> None:
    out = await evaluate(workspace(tmp_path), {"service/leak.py": line + "\n"})
    assert out["SEC-001-secrets"] is PolicyOutcome.BLOCK


async def test_secret_scan_respects_allowlist_marker(tmp_path: Path) -> None:
    line = 'BLOB = "q8Zr1Lp0Xk7Vb2Nm4Wc9Ty6Hs3Jd5Fg"  # pragma: allowlist secret\n'
    out = await evaluate(workspace(tmp_path), {"service/ok.py": line})
    assert out["SEC-001-secrets"] is PolicyOutcome.PASS
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("") == 0.0


async def test_dangerous_calls_and_bandit_block(tmp_path: Path) -> None:
    code = (
        "import subprocess\n\n\n"
        "def run(cmd: str) -> None:\n"
        "    subprocess.run(cmd, shell=True)\n"
        "    eval(cmd)\n"
    )
    out = await evaluate(workspace(tmp_path), {"service/danger.py": code})
    assert out["SEC-003-dangerous-calls"] is PolicyOutcome.BLOCK
    assert out["SEC-002-bandit"] is PolicyOutcome.BLOCK


async def test_pii_in_logs_is_blocked(tmp_path: Path) -> None:
    code = (
        "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"
        "def f(client_ip: str) -> None:\n"
        '    logger.info("redirect from %s", client_ip)\n'
    )
    out = await evaluate(workspace(tmp_path), {"service/log.py": code})
    assert out["CMP-001-no-pii-in-logs"] is PolicyOutcome.BLOCK
    clean = 'import logging\n\nlogger = logging.getLogger(__name__)\nlogger.info("ok %s", 1)\n'
    out2 = await evaluate(workspace(tmp_path / "b"), {"service/log.py": clean})
    assert out2["CMP-001-no-pii-in-logs"] is PolicyOutcome.PASS


async def test_dependency_allowlist_and_license(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    (ws.root / "pyproject.toml").write_text("[project]\ndependencies = [\n]\n")
    ok = await evaluate(
        ws, {"pyproject.toml": '[project]\ndependencies = [\n    "redis>=5.0",\n]\n'}
    )
    assert ok["SEC-004-dependency-allowlist"] is PolicyOutcome.PASS
    assert ok["CMP-002-license-allowlist"] is PolicyOutcome.PASS  # redis is MIT
    bad = await evaluate(
        ws, {"pyproject.toml": '[project]\ndependencies = [\n    "leftpad-ng>=1.0",\n]\n'}
    )
    assert bad["SEC-004-dependency-allowlist"] is PolicyOutcome.BLOCK
    assert bad["CMP-002-license-allowlist"] is PolicyOutcome.REQUIRE_APPROVAL
    assert bad["SEC-005-known-cves"] is PolicyOutcome.BLOCK  # cannot audit an unknown package


async def test_analytics_without_retention_setting_is_blocked(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    out = await evaluate(ws, {"service/analytics.py": "CLICKS: list[int] = []\n"})
    assert out["CMP-003-analytics-retention"] is PolicyOutcome.BLOCK
    out2 = await evaluate(
        ws,
        {"service/config.py": "class Settings:\n    analytics_retention_days: int = 90\n"},
    )
    assert out2["CMP-003-analytics-retention"] is PolicyOutcome.PASS


async def test_irreversible_migration_is_blocked(tmp_path: Path) -> None:
    base = "service/migrations/versions/rev_0002_x.py"
    good = "def upgrade() -> None:\n    op.add_column('a')\n\n\ndef downgrade() -> None:\n    op.drop_column('a')\n"
    empty = (
        "def upgrade() -> None:\n    op.add_column('a')\n\n\ndef downgrade() -> None:\n    pass\n"
    )
    missing = "def upgrade() -> None:\n    op.add_column('a')\n"
    assert (await evaluate(workspace(tmp_path / "1"), {base: good}))[
        "CHG-003-reversible-migrations"
    ] is PolicyOutcome.PASS
    for i, code in enumerate((empty, missing)):
        out = await evaluate(workspace(tmp_path / f"x{i}"), {base: code})
        assert out["CHG-003-reversible-migrations"] is PolicyOutcome.BLOCK


async def test_diff_size_and_write_path_policies(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    big = "\n".join(f"X{i} = {i}" for i in range(2600)) + "\n"
    out = await evaluate(ws, {"service/huge.py": big})
    assert out["CHG-002-diff-size"] is PolicyOutcome.BLOCK
    # The workspace refuses denylisted writes; the policy independently flags them as evidence.
    applied = AppliedChange(files=["policies/security.yaml", "setup.cfg"])
    results = await ENGINE.evaluate(PolicyInput(node=node("n"), applied=applied, workspace=ws))
    by_id = {r.policy_id: r for r in results}
    assert by_id["CHG-001-write-paths"].outcome is PolicyOutcome.BLOCK
    assert len(by_id["CHG-001-write-paths"].evidence) == 2


async def test_release_stage_requires_changelog_entry(tmp_path: Path) -> None:
    ws = workspace(tmp_path)
    out = await evaluate(ws, {}, stage="release", type_=TaskType.RELEASE_CHECK)
    assert out["CHG-004-changelog"] is PolicyOutcome.BLOCK
    (ws.root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n### Added\n- x\n")
    out2 = await evaluate(ws, {}, stage="release", type_=TaskType.RELEASE_CHECK)
    assert out2["CHG-004-changelog"] is PolicyOutcome.PASS


async def test_engine_fails_closed_on_crashing_check_and_rejects_unknown_checks() -> None:
    from orchestrator.policy import engine as eng

    async def boom(inp: PolicyInput, params: dict[str, object]) -> list[str]:
        raise RuntimeError("bug")

    eng.CHECKS["boom"] = boom
    try:
        rule = PolicyRule("X-1", "test", "boom", PolicyOutcome.BLOCK, "d", ["node"], "low", {})  # type: ignore[arg-type]
        results = await PolicyEngine([rule]).evaluate(
            PolicyInput(node=node("n"), applied=None, workspace=None)
        )
        assert results[0].outcome is PolicyOutcome.BLOCK
        assert "crashed" in results[0].evidence[0]
    finally:
        del eng.CHECKS["boom"]
    with pytest.raises(ValueError, match="unknown checks"):
        PolicyEngine([PolicyRule("Y", "t", "nope", PolicyOutcome.WARN, "", ["node"], "low", {})])  # type: ignore[arg-type]
