"""CLI commands and report generation over a small fake-agent run."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orchestrator import cli
from orchestrator.models import RunState
from orchestrator.report import write_reports
from tests.orchestrator.fakes import FakeAgent, Step, make_orchestrator, node

runner = CliRunner()


@pytest.fixture
async def finished_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agent = FakeAgent(
        {
            "a": [Step(ok=False), Step()],
            "b": [Step(approval={"reasons": ["needs eyes"], "risk": "medium"})],
        }
    )
    orch = make_orchestrator(tmp_path, [node("a"), node("b", ["a"])], agent)
    assert await orch.start() is RunState.PAUSED
    write_reports(orch)
    monkeypatch.setattr(cli, "RUNS", tmp_path / "runs")
    return orch.run_dir


def test_status_runs_metrics_and_audit(finished_run: Path) -> None:
    status = runner.invoke(cli.app, ["status", "run-test"])
    assert status.exit_code == 0
    assert "AWAITING_APPROVAL" in status.stdout
    assert "b#1" in status.stdout
    assert "run-test" in runner.invoke(cli.app, ["runs"]).stdout

    metrics = runner.invoke(cli.app, ["metrics", "run-test"])
    assert json.loads(metrics.stdout)["retries"] == 1
    allruns = json.loads(runner.invoke(cli.app, ["metrics"]).stdout)
    assert allruns["aggregate"]["runs"] == 1
    prom = runner.invoke(cli.app, ["metrics", "--format", "prometheus"]).stdout
    assert "orchestrator_retry_rate" in prom

    ok = runner.invoke(cli.app, ["audit", "verify", "run-test"])
    assert ok.exit_code == 0
    assert "intact" in ok.stdout
    audit = finished_run / "audit.jsonl"
    lines = audit.read_text().splitlines()
    event = json.loads(lines[3])
    event["payload"]["tampered"] = True
    lines[3] = json.dumps(event)
    audit.write_text("\n".join(lines) + "\n")
    broken = runner.invoke(cli.app, ["audit", "verify", "run-test"])
    assert broken.exit_code == 1
    assert "BROKEN" in broken.stdout


def test_unknown_run_and_gate_errors(finished_run: Path) -> None:
    assert runner.invoke(cli.app, ["status", "nope"]).exit_code == 2
    assert runner.invoke(cli.app, ["approve", "run-test", "zzz"]).exit_code == 2


def test_stop_writes_kill_switch(finished_run: Path) -> None:
    result = runner.invoke(cli.app, ["stop", "run-test", "--reason", "maintenance window"])
    assert result.exit_code == 0
    assert (finished_run / "STOP").read_text() == "maintenance window"


def test_reports_contain_every_section(finished_run: Path) -> None:
    md = (finished_run / "report.md").read_text()
    for section in (
        "## Plan DAG",
        "## Timeline",
        "## Gates",
        "## Approvals",
        "## Decisions",
        "## Metrics",
        "## Artifacts",
    ):
        assert section in md
    assert "```mermaid" in md
    html = (finished_run / "report.html").read_text()
    assert html.startswith("<!doctype html>")
    assert "<svg" in html
    assert "prefers-color-scheme: dark" in html
    assert "<script" not in html  # self-contained, no external JS
    for name in ("metrics.json", "decisions.json", "traces.jsonl", "plan.mmd"):
        assert (finished_run / name).exists()


def test_lineage_diff_detects_differences(tmp_path: Path) -> None:
    ws, repo = tmp_path / "ws", tmp_path / "repo"
    for root in (ws, repo):
        (root / "service").mkdir(parents=True)
        (root / "service" / "a.py").write_text("x = 1\n")
        (root / "docs" / "service").mkdir(parents=True)
    (ws / "README.md").write_text("r\n")
    (repo / "docs" / "service" / "README.md").write_text("r\n")
    assert cli.lineage_diff(ws, repo) == []
    (ws / "service" / "a.py").write_text("x = 2\n")
    (ws / "service" / "new.py").write_text("")
    assert cli.lineage_diff(ws, repo) == ["service/new.py", "service/a.py"]
