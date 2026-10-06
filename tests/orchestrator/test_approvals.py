from pathlib import Path

import pytest

from orchestrator.approvals import (
    AutoApprovePolicy,
    AutonomyPolicy,
    approver_identity,
    evaluate_autonomy,
)
from orchestrator.models import RiskLevel, TaskType
from orchestrator.workspace import AppliedChange
from tests.orchestrator.fakes import node

ROOT = Path(__file__).resolve().parents[2]
POLICY = AutonomyPolicy.load(ROOT / "policies" / "autonomy.yaml")


def change(files: dict[str, list[str]], deleted: list[str] | None = None) -> AppliedChange:
    diff = []
    for path, lines in files.items():
        diff.append(f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@")
        diff += [f"+{ln}" for ln in lines]
    return AppliedChange(
        files=list(files),
        deleted=deleted or [],
        diff="\n".join(diff),
        lines_added=sum(len(v) for v in files.values()),
    )


def test_plain_service_change_is_autonomous() -> None:
    need = evaluate_autonomy(POLICY, node("impl"), change({"service/links.py": ["x = 1"]}))
    assert need is None


def test_additive_migration_needs_medium_and_destructive_needs_high() -> None:
    mig = "service/migrations/versions/rev_0002.py"
    additive = evaluate_autonomy(POLICY, node("m"), change({mig: ["op.create_table('clicks')"]}))
    assert additive is not None
    assert additive.risk is RiskLevel.MEDIUM
    assert "db_migration" in additive.reasons[0]
    destructive = evaluate_autonomy(POLICY, node("m"), change({mig: ["op.drop_column('x')"]}))
    assert destructive is not None
    assert destructive.risk is RiskLevel.HIGH
    assert "destructive" in destructive.reasons[0]


def test_dependency_auth_deletion_size_and_release_triggers() -> None:
    dep = evaluate_autonomy(POLICY, node("d"), change({"pyproject.toml": ['    "redis>=5.0",']}))
    assert dep is not None and dep.risk is RiskLevel.MEDIUM
    # editing pyproject without touching dependency lines is fine
    assert evaluate_autonomy(POLICY, node("d"), change({"pyproject.toml": ["[tool.x]"]})) is None

    auth = evaluate_autonomy(POLICY, node("a"), change({"service/auth.py": ["def verify(): ..."]}))
    assert auth is not None and auth.risk is RiskLevel.HIGH

    deletion = evaluate_autonomy(
        POLICY, node("x"), change({"docs/a.md": []}, deleted=["docs/a.md"])
    )
    assert deletion is not None and "file_deletion" in deletion.reasons[0]

    big = evaluate_autonomy(POLICY, node("x"), change({"service/big.py": ["x"] * 1300}))
    assert big is not None and "large_change" in big.reasons[0]

    release = node("release", type_=TaskType.RELEASE_CHECK)
    low_run = evaluate_autonomy(POLICY, release, None, run_risk=RiskLevel.LOW)
    high_run = evaluate_autonomy(POLICY, release, None, run_risk=RiskLevel.HIGH)
    assert low_run is not None and low_run.risk is RiskLevel.MEDIUM
    assert high_run is not None and high_run.risk is RiskLevel.HIGH


def test_high_risk_write_node_always_needs_approval() -> None:
    n = node("impl", risk="high", write_paths=["service/**"])
    need = evaluate_autonomy(POLICY, n, change({"service/x.py": ["y"]}))
    assert need is not None and need.risk is RiskLevel.HIGH


@pytest.mark.parametrize(
    ("spec", "flag", "risk", "expected"),
    [
        ("low,medium", False, RiskLevel.LOW, True),
        ("low,medium", False, RiskLevel.MEDIUM, True),
        ("low,medium", False, RiskLevel.HIGH, False),
        ("low,medium,high", False, RiskLevel.HIGH, False),  # listing high is not enough
        ("low,medium", True, RiskLevel.HIGH, True),
        ("", False, RiskLevel.LOW, False),
        (None, False, RiskLevel.MEDIUM, False),
    ],
)
def test_auto_approve_policy(spec: str | None, flag: bool, risk: RiskLevel, expected: bool) -> None:
    policy = AutoApprovePolicy.parse(spec, allow_high_risk=flag)
    ok, why = policy.decide(risk, POLICY.never_auto_approve)
    assert ok is expected
    assert why


def test_approver_identity_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_APPROVER", "carol@example.com")
    assert approver_identity() == "carol@example.com"
    monkeypatch.delenv("ORCH_APPROVER")
    assert approver_identity()  # git email or OS user
