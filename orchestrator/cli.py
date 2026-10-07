"""``orchestrator`` command-line interface (Typer + Rich)."""

from __future__ import annotations

import asyncio
import filecmp
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table

from orchestrator import runner
from orchestrator.audit import verify_chain
from orchestrator.controls import KillSwitch
from orchestrator.engine import RunConfig
from orchestrator.metrics import aggregate, collect_runs, run_metrics, to_prometheus
from orchestrator.models import ApprovalRequest, ApprovalStatus, RunState
from orchestrator.state import StateStore

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Agentic SDLC orchestrator")
audit_app = typer.Typer(no_args_is_help=True, help="Audit log commands")
app.add_typer(audit_app, name="audit")
console = Console()

RUNS = Path("runs")
WORK = Path(".work")
LINEAGE_PATHS = [
    "service",
    "tests/service",
    "docs/openapi.json",
    "docs/service",
    "scripts/loadcheck.py",
]

AutoApprove = Annotated[
    str | None,
    typer.Option("--auto-approve", help="Risk levels approved automatically, e.g. low,medium"),
]
HighRisk = Annotated[
    bool,
    typer.Option("--i-understand-high-risk", help="Also auto-approve HIGH-risk gates (audited)"),
]
Interactive = Annotated[
    bool, typer.Option("--interactive", help="Prompt for approvals in the terminal")
]
Provider = Annotated[
    str | None,
    typer.Option(
        help="deterministic | anthropic | replay | record "
        "(default: anthropic if ANTHROPIC_API_KEY is set)"
    ),
]


def _run_dir(run_id: str) -> Path:
    path = RUNS / run_id
    if not (path / "state.db").exists():
        console.print(f"[red]no run named {run_id} in {RUNS}/[/red]")
        raise typer.Exit(2)
    return path


async def _prompt(request: ApprovalRequest) -> tuple[bool, str]:
    console.rule(f"[bold]Approval required: {request.gate_id} ({request.risk.value} risk)")
    for reason in request.reasons:
        console.print(f" • {reason}")
    approved = Confirm.ask("Approve?", default=False)
    comment = Prompt.ask("Comment", default="approved interactively" if approved else "rejected")
    return approved, comment


def _config(
    *,
    provider: str | None,
    auto_approve: str | None,
    high_risk: bool,
    interactive: bool,
    scenario: str | None = None,
    events: str | None = None,
    model: str | None = None,
) -> RunConfig:
    return RunConfig.model_validate(
        {
            "provider": provider or runner.default_provider(),
            "model": model,
            "auto_approve": auto_approve,
            "allow_high_risk": high_risk,
            "interactive": interactive,
            "scenario": scenario,
            "events_path": events,
            "retry": {"base_delay_s": 0.2, "max_delay_s": 2.0},
        }
    )


def _print_outcome(run_dir: Path, state: RunState) -> None:
    m = runner.run_metrics_summary(run_dir)
    color = {"SUCCEEDED": "green", "PAUSED": "yellow"}.get(state.value, "red")
    console.print(
        f"[bold {color}]{state.value}[/] run [cyan]{run_dir.name}[/] · "
        f"nodes {m['nodes']['SUCCEEDED']}/{m['nodes_total']} ok · retries {m['retries']} · "
        f"rollbacks {m['rollbacks']} · "
        f"approvals {m['approvals']} · replans {m['replans']} · {m['end_to_end_latency_s']}s"
    )
    console.print(f"  report: {run_dir / 'report.md'}  (html: {run_dir / 'report.html'})")
    if state is RunState.PAUSED:
        store = StateStore(run_dir / "state.db")
        for a in store.approvals(ApprovalStatus.PENDING):
            console.print(
                f"  [yellow]pending[/] {a.gate_id} ({a.risk.value}): {a.reasons[0][:140]}\n"
                f"    orchestrator approve {run_dir.name} '{a.gate_id}' --comment '...'  "
                f"&& orchestrator resume {run_dir.name}"
            )
        store.close()


def _start(
    requirement: Path, workspace: Path, config: RunConfig, run_id: str
) -> tuple[Path, RunState]:
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    orch = runner.build(
        run_dir,
        workspace=workspace,
        config=config,
        requirement_name=requirement.name,
        prompter=_prompt if config.interactive else None,
    )
    orch.create(
        requirement_path=requirement,
        bootstrap_nodes=runner.bootstrap_nodes(),
        scenario=config.scenario,
    )
    console.print(f"[bold]run[/] {run_id}  workspace={workspace}  provider={config.provider}")
    return run_dir, asyncio.run(orch.start())


@app.command()
def run(
    requirement: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    workspace: Annotated[Path, typer.Option(help="Target repository (created if missing)")],
    auto_approve: AutoApprove = None,
    high_risk: HighRisk = False,
    interactive: Interactive = False,
    provider: Provider = None,
    model: Annotated[str | None, typer.Option(help="Claude model id")] = None,
    events: Annotated[Path | None, typer.Option(help="Scripted mid-run events (YAML)")] = None,
) -> None:
    """Run a requirement (YAML or free text) against a workspace."""
    if not workspace.exists():
        runner.init_scaffold(workspace)
    config = _config(
        provider=provider, auto_approve=auto_approve, high_risk=high_risk, interactive=interactive,
        events=str(events) if events else None, model=model,
    )  # fmt: skip
    run_dir, state = _start(
        requirement, workspace.resolve(), config, runner.new_run_id(requirement.stem)
    )
    _print_outcome(run_dir, state)
    raise typer.Exit(0 if state in {RunState.SUCCEEDED, RunState.PAUSED} else 1)


def run_scenario(
    name: str,
    *,
    workspace: Path | None,
    auto_approve: str | None,
    high_risk: bool,
    interactive: bool,
    provider: str | None,
) -> tuple[Path, RunState, Path]:
    meta = runner.SCENARIOS[name]
    if workspace is None:
        workspace = WORK / name
        after = meta["after"]
        if after is None:
            runner.init_scaffold(workspace)
        else:
            source = runner.last_good_workspace(WORK, after)
            if source is None:
                console.print(f"[dim]prerequisite: running scenario '{after}' first[/dim]")
                _, prev_state, source = run_scenario(
                    after, workspace=None, auto_approve=auto_approve, high_risk=True,
                    interactive=False, provider=provider,
                )  # fmt: skip
                if prev_state is not RunState.SUCCEEDED:
                    console.print(f"[red]prerequisite {after} ended {prev_state.value}[/red]")
                    raise typer.Exit(1)
            runner.copy_workspace(source, workspace)
    elif not workspace.exists():
        runner.init_scaffold(workspace)
    config = _config(
        provider=provider, auto_approve=auto_approve, high_risk=high_risk, interactive=interactive,
        scenario=name, events=meta["events"],
    )  # fmt: skip
    requirement = runner.REPO_ROOT / str(meta["requirement"])
    run_id = runner.new_run_id(name)
    run_dir, state = _start(requirement, workspace.resolve(), config, run_id)
    runner.mark_workspace(workspace, name, run_id, state.value)
    return run_dir, state, workspace


@app.command()
def scenario(
    name: Annotated[str, typer.Argument(help="greenfield | brownfield | ambiguous")],
    auto_approve: AutoApprove = None,
    high_risk: HighRisk = False,
    interactive: Interactive = False,
    provider: Provider = None,
    workspace: Annotated[
        Path | None, typer.Option(help="Reuse a workspace instead of .work/<name>")
    ] = None,
) -> None:
    """Run one of the three bundled scenarios with one command."""
    if name not in runner.SCENARIOS:
        console.print(f"[red]unknown scenario {name}; choose {list(runner.SCENARIOS)}[/red]")
        raise typer.Exit(2)
    run_dir, state, _ = run_scenario(
        name, workspace=workspace, auto_approve=auto_approve, high_risk=high_risk,
        interactive=interactive, provider=provider,
    )  # fmt: skip
    _print_outcome(run_dir, state)
    raise typer.Exit(0 if state in {RunState.SUCCEEDED, RunState.PAUSED} else 1)


@app.command()
def demo(
    provider: Provider = None,
    workspace: Annotated[Path, typer.Option(help="Demo workspace")] = WORK / "demo",
) -> None:
    """Run greenfield -> brownfield -> ambiguous in one workspace with auto-approval
    (low,medium plus the explicit high-risk flag) and print a summary."""
    started = time.monotonic()
    runner.init_scaffold(workspace)
    rows = []
    for name in runner.SCENARIOS:
        console.rule(f"[bold]{name}")
        run_dir, state, _ = run_scenario(
            name, workspace=workspace, auto_approve="low,medium", high_risk=True,
            interactive=False, provider=provider,
        )  # fmt: skip
        _print_outcome(run_dir, state)
        rows.append((name, run_dir, state, runner.run_metrics_summary(run_dir)))
        if state is not RunState.SUCCEEDED:
            break
    table = Table(title=f"Demo summary ({time.monotonic() - started:.0f}s)")
    for col in ("scenario", "run", "state", "nodes ok", "retries", "rollbacks", "approvals (auto)",
                "replans", "policy blocks", "e2e s", "audit chain"):  # fmt: skip
        table.add_column(col)
    for name, run_dir, state, m in rows:
        table.add_row(
            name, run_dir.name, state.value, f"{m['nodes']['SUCCEEDED']}/{m['nodes_total']}",
            str(m["retries"]), str(m["rollbacks"]), f"{m['approvals']} ({m['approvals_auto']})",
            str(m["replans"]), str(m["policy_violations"]["block"]), str(m["end_to_end_latency_s"]),
            "intact" if m["audit_chain_ok"] else "BROKEN",
        )  # fmt: skip
    console.print(table)
    ok = all(state is RunState.SUCCEEDED for _, _, state, _ in rows) and len(rows) == 3
    if ok:
        mismatches = lineage_diff(workspace, runner.REPO_ROOT)
        console.print(
            "committed service/ matches orchestrator output: "
            + (
                "[green]yes[/green]"
                if not mismatches
                else f"[yellow]no ({len(mismatches)} files differ)[/yellow]"
            )
        )
    raise typer.Exit(0 if ok else 1)


@app.command()
def resume(run_id: str, interactive: Interactive = False) -> None:
    """Continue a paused, safe-stopped or crashed run."""
    run_dir = _run_dir(run_id)
    orch = runner.open_run(run_dir, prompter=_prompt if interactive else None)
    if interactive:
        orch.config.interactive = True
    state = asyncio.run(orch.resume())
    _print_outcome(run_dir, state)
    store = StateStore(run_dir / "state.db")
    if store.run()["scenario"]:
        runner.mark_workspace(
            Path(store.run()["workspace"]), store.run()["scenario"], run_id, state.value
        )
    raise typer.Exit(0 if state in {RunState.SUCCEEDED, RunState.PAUSED} else 1)


@app.command()
def stop(run_id: str, reason: Annotated[str, typer.Option()] = "operator requested stop") -> None:
    """Kill switch: the run finishes in-flight nodes, persists state and stops (SAFE_STOPPED)."""
    KillSwitch(_run_dir(run_id)).trigger(reason)
    console.print(
        f"STOP written for {run_id}; the engine will safe-stop at the next scheduling tick."
    )


def _decide(run_id: str, gate_id: str, approved: bool, comment: str) -> None:
    run_dir = _run_dir(run_id)
    orch = runner.open_run(run_dir)
    try:
        decided = orch.approve(gate_id, approved=approved, comment=comment)
    except (KeyError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(
        f"{decided.status.value} {gate_id} by {decided.approver} "
        f"(diff {decided.diff_hash[:12]}). Run `orchestrator resume {run_id}` to continue."
    )


@app.command()
def approve(
    run_id: str, gate_id: str, comment: Annotated[str, typer.Option()] = "approved"
) -> None:
    """Approve a pending gate (identity from ORCH_APPROVER or git config)."""
    _decide(run_id, gate_id, True, comment)


@app.command()
def reject(run_id: str, gate_id: str, comment: Annotated[str, typer.Option()] = "rejected") -> None:
    """Reject a pending gate; the node is rolled back and retried with the comment as feedback."""
    _decide(run_id, gate_id, False, comment)


@app.command()
def status(run_id: str) -> None:
    """Node states and pending approvals of a run."""
    store = StateStore(_run_dir(run_id) / "state.db")
    run = store.run()
    table = Table(
        title=f"{run_id} - {run['state']} (risk {run['risk']}, plan v{run['plan_version']})"
    )
    for col in ("node", "type", "agent", "risk", "state", "attempts"):
        table.add_column(col)
    for row, node in zip(store.node_rows(), store.nodes(), strict=True):
        table.add_row(
            node.id,
            node.type.value,
            node.agent,
            node.risk.value,
            row["state"],
            str(row["attempts"]),
        )
    console.print(table)
    for a in store.approvals(ApprovalStatus.PENDING):
        console.print(
            f"[yellow]pending approval[/] {a.gate_id} ({a.risk.value}): {'; '.join(a.reasons)}"
        )


@app.command("runs")
def list_runs() -> None:
    """List runs under runs/."""
    table = Table()
    for col in ("run", "scenario", "state", "risk", "created"):
        table.add_column(col)
    for db in sorted(RUNS.glob("*/state.db")):
        store = StateStore(db)
        r = store.run()
        table.add_row(r["run_id"], str(r["scenario"]), r["state"], r["risk"], r["created_at"][:19])
        store.close()
    console.print(table)


@audit_app.command("verify")
def audit_verify(run_id: str) -> None:
    """Verify the hash chain of a run's audit log."""
    result = verify_chain(_run_dir(run_id) / "audit.jsonl")
    if result.ok:
        console.print(f"[green]audit chain intact[/green]: {result.events} events ({run_id})")
        return
    console.print(f"[red]audit chain BROKEN at seq {result.bad_seq}: {result.error}[/red]")
    raise typer.Exit(1)


@app.command()
def metrics(
    run_id: Annotated[str | None, typer.Argument()] = None,
    fmt: Annotated[str, typer.Option("--format", help="json | prometheus")] = "json",
) -> None:
    """Reliability metrics for one run or across all runs."""
    if run_id:
        store = StateStore(_run_dir(run_id) / "state.db")
        runs = [run_metrics(store)]
    else:
        runs = collect_runs(RUNS)
    if fmt == "prometheus":
        sys.stdout.write(to_prometheus(runs))
        return
    payload = runs[0] if run_id else {"aggregate": aggregate(runs), "runs": runs}
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


@app.command()
def report(run_id: str) -> None:
    """Regenerate report.md / report.html for a run."""
    from orchestrator.report import write_reports  # noqa: PLC0415

    run_dir = _run_dir(run_id)
    write_reports(runner.open_run(run_dir))
    console.print(f"wrote {run_dir / 'report.md'} and {run_dir / 'report.html'}")


@app.command()
def trace(
    run_id: str, target: Annotated[str, typer.Argument(help="path[:line] in the workspace")]
) -> None:
    """Trace a file (or line) back through node, decisions and the requirement."""
    run_dir = _run_dir(run_id)
    orch = runner.open_run(run_dir)
    path, _, line = target.partition(":")
    node_id: str | None = None
    decision_id: str | None = None
    if line and orch.ws is not None:
        sha = asyncio.run(orch.ws.blame_commit(path, int(line)))
        if sha:
            message = asyncio.run(orch.ws.commit_message(sha))
            fields = dict(
                ln.split(": ", 1)
                for ln in message.splitlines()
                if ": " in ln and ln.split(": ")[0] in {"run", "node", "decision"}
            )
            console.print(
                f"line {line} of {path} last changed by commit {sha[:10]} "
                f"(run {fields.get('run', '?')})"
            )
            if fields.get("run") == run_id:
                node_id, decision_id = fields.get("node"), fields.get("decision")
            else:
                console.print(
                    f"[yellow]that commit belongs to run {fields.get('run')}; "
                    "trace it there[/yellow]"
                )
                return
    if node_id is None:
        producers = [
            a for a in orch.store.artifacts() if path in a.paths and a.key.startswith("changes:")
        ]
        if not producers:
            console.print(f"[red]{path} was not produced in run {run_id}[/red]")
            raise typer.Exit(1)
        node_id = producers[-1].node_id
        decision_id = orch.decisions.latest_for_nodes([node_id])[0]
    console.print(f"[bold]{path}[/bold] <- node [cyan]{node_id}[/cyan]")
    assert decision_id is not None  # noqa: S101
    for d in orch.decisions.lineage(decision_id):
        console.print(
            f"  {d.decision_id} " + escape(f"[{d.node_id} / {d.agent}]") + f" {d.rationale[:150]}"
        )


def lineage_diff(workspace: Path, repo: Path) -> list[str]:
    """Files that differ between the orchestrator workspace and the committed repo."""
    diffs: list[str] = []
    for rel in LINEAGE_PATHS:
        a, b = workspace / rel, repo / rel
        if a.is_file() or b.is_file():
            if not (a.is_file() and b.is_file() and filecmp.cmp(a, b, shallow=False)):
                diffs.append(rel)
            continue
        files_a = (
            {
                p.relative_to(a).as_posix()
                for p in a.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            }
            if a.exists()
            else set()
        )
        files_b = (
            {
                p.relative_to(b).as_posix()
                for p in b.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            }
            if b.exists()
            else set()
        )
        if rel == "docs/service":
            files_b.discard("README.md")  # the workspace README.md is synced here; checked below
        for f in sorted(files_a ^ files_b):
            diffs.append(f"{rel}/{f}")
        for f in sorted(files_a & files_b):
            if not filecmp.cmp(a / f, b / f, shallow=False):
                diffs.append(f"{rel}/{f}")
    if (workspace / "README.md").exists() and not (
        (repo / "docs/service/README.md").exists()
        and filecmp.cmp(workspace / "README.md", repo / "docs/service/README.md", shallow=False)
    ):
        diffs.append("docs/service/README.md")
    return diffs


@app.command()
def sync(source: Annotated[Path, typer.Option("--from")] = WORK / "demo") -> None:
    """Copy the orchestrator-produced service, tests and docs from a workspace into the repo."""
    repo = runner.REPO_ROOT
    for rel in LINEAGE_PATHS:
        src, dst = source / rel, repo / rel
        if dst.is_dir():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    shutil.copyfile(source / "README.md", repo / "docs/service/README.md")
    console.print(f"synced {LINEAGE_PATHS} + README from {source}")


@app.command("verify-lineage")
def verify_lineage(workspace: Annotated[Path, typer.Option()] = WORK / "demo") -> None:
    """Fail if the committed service differs from what the orchestrator produced."""
    if not workspace.exists():
        console.print(f"[red]{workspace} does not exist; run `make demo` first[/red]")
        raise typer.Exit(2)
    diffs = lineage_diff(workspace, runner.REPO_ROOT)
    if diffs:
        console.print(f"[red]{len(diffs)} file(s) differ from orchestrator output:[/red]")
        for d in diffs[:50]:
            console.print(f"  {d}")
        raise typer.Exit(1)
    console.print(
        "[green]committed service/, tests/service/ and docs match the orchestrator output "
        "byte-for-byte[/green]"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
