"""Run reports: ``report.md`` and a self-contained ``report.html`` plus machine-readable exports.

Written by the engine's finalizer at the end of every execution (also when paused or stopped),
and on demand via ``orchestrator report <run_id>``.
"""

from __future__ import annotations

import html
import json
import subprocess  # nosec B404 - fixed argv git invocation only
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator import dag
from orchestrator.audit import verify_chain
from orchestrator.metrics import run_metrics
from orchestrator.models import NodeState, RequirementSpec

if TYPE_CHECKING:
    from orchestrator.engine import Orchestrator


def _git_diff(workspace: str, base: str | None) -> str:
    if not base or not Path(workspace, ".git").exists():
        return ""
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "diff", base, "HEAD", "--", ".", ":(exclude)docs/openapi.json"],  # noqa: S607
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return res.stdout


def collect(orch: Orchestrator) -> dict[str, Any]:
    store = orch.store
    run = store.run()
    nodes = store.nodes()
    rows = {r["node_id"]: r for r in store.node_rows()}
    states = dict(store.node_states())
    spec_art = orch.context.get("spec")
    events = orch.audit.events()
    return {
        "run": run,
        "nodes": nodes,
        "rows": rows,
        "states": states,
        "spec": RequirementSpec.model_validate(spec_art.content) if spec_art else None,
        "metrics": run_metrics(store),
        "gates": store.gate_results(),
        "policies": store.policy_results(),
        "approvals": store.approvals(),
        "decisions": store.decisions(),
        "artifacts": store.artifacts(),
        "events": events,
        "chain": verify_chain(orch.run_dir / "audit.jsonl"),
        "replans": [e for e in events if e["type"] == "replanned"],
        "notable": [
            e
            for e in events
            if e["type"]
            in {
                "rollback",
                "retry_scheduled",
                "llm_fallback",
                "safe_stop",
                "budget_breach",
                "change_detected",
                "scripted_event",
                "high_risk_override",
                "approval_invalidated",
                "crash_recovery_rollback",
            }
        ],
    }


def write_reports(orch: Orchestrator) -> None:
    data = collect(orch)
    out = orch.run_dir
    (out / "metrics.json").write_text(json.dumps(data["metrics"], indent=2) + "\n")
    (out / "decisions.json").write_text(
        json.dumps([d.model_dump(mode="json") for d in data["decisions"]], indent=2) + "\n"
    )
    (out / "plan.mmd").write_text(dag.to_mermaid(data["nodes"], data["states"]) + "\n")
    orch.tracer.export_jsonl(out / "traces.jsonl")
    patch = _git_diff(data["run"]["workspace"], orch.store.kv_get("baseline_commit"))
    if patch:
        (out / "changes.patch").write_text(patch)
    (out / "report.md").write_text(render_markdown(data))
    (out / "report.html").write_text(render_html(data))


def _dur(a: str | None, b: str | None) -> str:
    if not a or not b:
        return "-"
    return f"{(datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds():.1f}s"


def _cell(text: Any, limit: int = 160) -> str:
    s = " ".join(str(text).split())
    s = s if len(s) <= limit else s[: limit - 1] + "…"
    return s.replace("|", "\\|")


def render_markdown(d: dict[str, Any]) -> str:
    run, m, spec = d["run"], d["metrics"], d["spec"]
    lines = [
        f"# Run report: `{run['run_id']}`",
        "",
        f"- **Scenario:** {run['scenario'] or '-'}  ",
        f"- **State:** {run['state']}  ",
        f"- **Risk class:** {run['risk']}  ",
        f"- **Provider:** {run['config'].get('provider')}  ",
        f"- **Plan version:** {run['plan_version']}  ",
        f"- **Audit chain:** {'intact' if d['chain'].ok else 'BROKEN: ' + str(d['chain'].error)} "
        f"({d['chain'].events} events)  ",
        f"- **End-to-end latency:** {m['end_to_end_latency_s']}s",
        "",
    ]
    if spec:
        lines += [
            "## Requirement",
            "",
            f"**{spec.id} - {spec.title}** (v{spec.version}, ambiguity {spec.ambiguity_score}, risk {spec.risk.value})",
            "",
            f"> {spec.goal}",
            "",
            f"Capabilities: {', '.join(spec.capabilities)}",
            "",
        ]
        if spec.ambiguities:
            lines += ["| Ambiguous term | Question | Default assumption |", "|---|---|---|"]
            lines += [
                f"| {a.term} | {_cell(a.question)} | {_cell(a.default_assumption, 220)} |"
                for a in spec.ambiguities
            ]
            lines.append("")
        if spec.open_questions:
            lines += ["Open questions: " + "; ".join(spec.open_questions), ""]
        if spec.assumptions:
            lines += ["Assumptions:", *[f"- {_cell(a, 300)}" for a in spec.assumptions], ""]
        lines += ["| AC | Given / When / Then | Verified by |", "|---|---|---|"]
        lines += [
            f"| {ac.id} | Given {_cell(ac.given, 60)}, when {_cell(ac.when, 60)}, then {_cell(ac.then, 90)} | "
            f"{'<br>'.join(f'`{v}`' for v in ac.verified_by)} |"
            for ac in spec.acceptance_criteria
        ]
        lines.append("")
    lines += ["## Plan DAG", "", "```mermaid", dag.to_mermaid(d["nodes"], d["states"]), "```", ""]
    lines += [
        "## Timeline",
        "",
        "| Node | Type | Agent | Risk | State | Attempts | Started | Duration |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for n in d["nodes"]:
        r = d["rows"][n.id]
        lines.append(
            f"| `{n.id}` | {n.type.value} | {n.agent} | {n.risk.value} | {r['state']} | {r['attempts']} | "
            f"{(r['started_at'] or '-')[11:19]} | {_dur(r['started_at'], r['finished_at'])} |"
        )
    lines += [
        "",
        "## Gates",
        "",
        "| Node | Attempt | Phase | Gate | Result | Details |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{g['node_id']}` | {g['attempt']} | {g['phase']} | {g['gate']} | {'PASS' if g['passed'] else 'FAIL'} | {_cell(g['details'])} |"
        for g in d["gates"]
        if g["gate"] != "inputs_available"
    ]
    bad = [p for p in d["policies"] if p["outcome"] != "pass"]
    lines += [
        "",
        "## Policy results",
        "",
        f"{len(d['policies'])} evaluations; {len(bad)} non-passing.",
        "",
    ]
    if bad:
        lines += ["| Node | Attempt | Policy | Outcome | Evidence |", "|---|---|---|---|---|"]
        lines += [
            f"| `{p['node_id']}` | {p['attempt']} | {p['policy_id']} | {p['outcome']} | {_cell('; '.join(p['evidence']))} |"
            for p in bad
        ]
        lines.append("")
    lines += [
        "## Approvals",
        "",
        "| Gate | Risk | Status | Approver | Auto | Comment | Reasons |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{a.gate_id}` | {a.risk.value} | {a.status.value} | {a.approver or '-'} | {a.auto} | {_cell(a.comment or '-', 80)} | {_cell('; '.join(a.reasons), 200)} |"
        for a in d["approvals"]
    ]
    if d["replans"]:
        lines += ["", "## Dynamic re-planning", ""]
        for e in d["replans"]:
            p = e["payload"]
            lines += [
                f"- **{p['replan_id']}** (plan v{p['plan_version']}): risk {p['old_risk']} → {p['new_risk']}; "
                f"added {p['added']}, removed {p['removed']}, modified {p['modified']}, re-run {p['invalidated']}"
            ]
    if d["notable"]:
        lines += ["", "## Notable events", "", "| Seq | Event | Details |", "|---|---|---|"]
        lines += [
            f"| {e['seq']} | {e['type']} | {_cell(json.dumps(e['payload']), 200)} |"
            for e in d["notable"]
        ]
    lines += [
        "",
        "## Decisions (lineage)",
        "",
        "| Decision | Node | Agent | Parents | Rationale |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {x.decision_id} | `{x.node_id}` | {x.agent} | {', '.join(x.parent_decision_ids)} | {_cell(x.rationale, 200)} |"
        for x in d["decisions"]
    ]
    lines += ["", "## Metrics", "", "| Metric | Value |", "|---|---|"]
    for key in (
        "node_success_rate", "attempts", "retries", "retry_rate", "rollbacks", "rollback_frequency",
        "fallbacks", "fallback_frequency", "mttr_s", "end_to_end_latency_s", "approvals",
        "approvals_auto", "approval_wait_mean_s", "gates_evaluated", "gates_failed", "replans",
        "llm_calls", "llm_input_tokens", "llm_output_tokens", "llm_cost_usd",
    ):  # fmt: skip
        lines.append(f"| {key} | {m[key]} |")
    lines.append(f"| stage_latency_s | {m['stage_latency_s']} |")
    lines += ["", "## Artifacts", "", "| Key | Version | Producer | Files |", "|---|---|---|---|"]
    for a in d["artifacts"]:
        if a.key.startswith("gates:"):
            continue
        files = ", ".join(f"`{p}`" for p in list(a.paths)[:6]) + (" …" if len(a.paths) > 6 else "")
        lines.append(f"| `{a.key}` | {a.version} | `{a.node_id}` | {files or '-'} |")
    lines += [
        "",
        "Other files in this folder: `audit.jsonl` (hash-chained), `state.db`, `decisions.json`, "
        "`metrics.json`, `traces.jsonl`, `plan.mmd`, `diffs/`, `changes.patch`, `report.html`.",
        "",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- HTML

_COLORS = {
    NodeState.SUCCEEDED: "var(--ok)",
    NodeState.FAILED: "var(--bad)",
    NodeState.BLOCKED: "var(--bad)",
    NodeState.AWAITING_APPROVAL: "var(--gate)",
    NodeState.SKIPPED: "var(--muted)",
}


def _svg_dag(d: dict[str, Any]) -> str:
    nodes = d["nodes"]
    layers = dag.generations(nodes)
    w, h, gx, gy = 190, 46, 40, 26
    pos: dict[str, tuple[int, int]] = {}
    for li, layer in enumerate(layers):
        for ni, nid in enumerate(layer):
            pos[nid] = (20 + li * (w + gx), 20 + ni * (h + gy))
    width = 40 + len(layers) * (w + gx)
    height = 40 + max(len(lyr) for lyr in layers) * (h + gy)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" role="img" aria-label="plan DAG">'
    ]
    for n in nodes:
        for dep in n.depends_on:
            if dep in pos and n.id in pos:
                x1, y1 = pos[dep][0] + w, pos[dep][1] + h // 2
                x2, y2 = pos[n.id][0], pos[n.id][1] + h // 2
                parts.append(
                    f'<path d="M{x1},{y1} C{x1 + 20},{y1} {x2 - 20},{y2} {x2},{y2}" '
                    'fill="none" stroke="var(--line)" stroke-width="1.2"/>'
                )
    for n in nodes:
        if n.id not in pos:
            continue
        x, y = pos[n.id]
        color = _COLORS.get(d["states"].get(n.id, NodeState.PENDING), "var(--muted)")
        parts.append(
            f'<g><title>{html.escape(n.title)}</title><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" '
            f'fill="var(--card)" stroke="{color}" stroke-width="2"/>'
            f'<text x="{x + 9}" y="{y + 19}" class="t1">{html.escape(n.id)}</text>'
            f'<text x="{x + 9}" y="{y + 36}" class="t2">{n.type.value} · {n.agent} · {n.risk.value}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_timeline(d: dict[str, Any]) -> str:
    rows = [(n, d["rows"][n.id]) for n in d["nodes"] if d["rows"][n.id]["started_at"]]
    if not rows:
        return "<p>No node has started.</p>"
    starts = [datetime.fromisoformat(r["started_at"]) for _, r in rows]
    ends = [datetime.fromisoformat(r["finished_at"] or r["started_at"]) for _, r in rows]
    t0, t1 = min(starts), max(ends)
    span = max((t1 - t0).total_seconds(), 0.001)
    label_w, bar_w, rh = 190, 640, 20
    parts = [
        f'<svg viewBox="0 0 {label_w + bar_w + 70} {len(rows) * rh + 30}" width="100%" role="img" aria-label="timeline">'
    ]
    for i, ((n, r), s, e) in enumerate(zip(rows, starts, ends, strict=True)):
        y = 10 + i * rh
        x = label_w + (s - t0).total_seconds() / span * bar_w
        wd = max((e - s).total_seconds() / span * bar_w, 2)
        color = _COLORS.get(NodeState(r["state"]), "var(--muted)")
        parts.append(f'<text x="0" y="{y + 13}" class="t2">{html.escape(n.id)}</text>')
        parts.append(
            f'<rect x="{x:.1f}" y="{y + 3}" width="{wd:.1f}" height="{rh - 7}" rx="3" fill="{color}" opacity="0.85"/>'
        )
        parts.append(
            f'<text x="{x + wd + 4:.1f}" y="{y + 13}" class="t2">{(e - s).total_seconds():.1f}s</text>'
        )
    parts.append(f'<text x="{label_w}" y="{len(rows) * rh + 26}" class="t2">0s</text>')
    parts.append(
        f'<text x="{label_w + bar_w - 30}" y="{len(rows) * rh + 26}" class="t2">{span:.1f}s</text></svg>'
    )
    return "".join(parts)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(_cell(c, 400))}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    )


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1f2328;--muted:#8b949e;--line:#c9ccd1;--ok:#1a7f37;--bad:#cf222e;--gate:#3e4cd8;--warn:#bf8700}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#7d8590;--line:#30363d;--ok:#3fb950;--bad:#f85149;--gate:#7c8cff;--warn:#d29922}}
:root[data-theme="dark"]{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#7d8590;--line:#30363d;--ok:#3fb950;--bad:#f85149;--gate:#7c8cff;--warn:#d29922}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:1200px;margin:0 auto}h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid var(--line);padding-bottom:4px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px}.kpi b{display:block;font-size:20px}
.kpi span{color:var(--muted);font-size:12px}.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}th,td{border:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top;font-size:12.5px}
th{background:var(--bg)}text.t1{font:600 12px system-ui;fill:var(--fg)}text.t2{font:11px system-ui;fill:var(--muted)}
.state{font-weight:700}.SUCCEEDED{color:var(--ok)}.FAILED,.SAFE_STOPPED{color:var(--bad)}.PAUSED{color:var(--gate)}
"""


def render_html(d: dict[str, Any]) -> str:
    run, m, spec = d["run"], d["metrics"], d["spec"]
    kpis = [
        ("State", run["state"]),
        ("Risk", run["risk"]),
        ("Nodes", f"{m['nodes']['SUCCEEDED']}/{m['nodes_total']} ok"),
        ("Retries", m["retries"]),
        ("Rollbacks", m["rollbacks"]),
        ("MTTR", f"{m['mttr_s']}s" if m["mttr_s"] is not None else "-"),
        ("Approvals", f"{m['approvals']} ({m['approvals_auto']} auto)"),
        ("Re-plans", m["replans"]),
        ("E2E", f"{m['end_to_end_latency_s']}s"),
        ("Audit chain", "intact" if d["chain"].ok else "BROKEN"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><span>{k}</span><b>{html.escape(str(v))}</b></div>' for k, v in kpis
    )
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Run {html.escape(run['run_id'])}</title><style>{CSS}</style></head><body><main>",
        f"<h1>Run <code>{html.escape(run['run_id'])}</code> <span class='state {run['state']}'>{run['state']}</span></h1>",
        f"<p>Scenario <b>{html.escape(str(run['scenario']))}</b> · provider {html.escape(str(run['config'].get('provider')))} · plan v{run['plan_version']}</p>",
        f'<div class="kpis">{kpi_html}</div>',
    ]
    if spec:
        parts += [
            f"<h2>Requirement {html.escape(spec.id)} (v{spec.version})</h2><p>{html.escape(spec.goal)}</p>",
            f"<p>Ambiguity score {spec.ambiguity_score} · risk {spec.risk.value} · capabilities {html.escape(', '.join(spec.capabilities))}</p>",
        ]
        if spec.ambiguities:
            parts.append(
                _table(
                    ["Term", "Question", "Default assumption"],
                    [[a.term, a.question, a.default_assumption] for a in spec.ambiguities],
                )
            )
        parts.append(
            _table(
                ["AC", "Then", "Verified by"],
                [[a.id, a.then, ", ".join(a.verified_by)] for a in spec.acceptance_criteria],
            )
        )
    parts += ["<h2>Plan DAG</h2>", f'<div class="scroll">{_svg_dag(d)}</div>']
    parts += ["<h2>Timeline</h2>", f'<div class="scroll">{_svg_timeline(d)}</div>']
    parts += [
        "<h2>Nodes</h2>",
        _table(
            ["Node", "Type", "Agent", "Risk", "State", "Attempts", "Duration"],
            [[n.id, n.type.value, n.agent, n.risk.value, d["rows"][n.id]["state"], d["rows"][n.id]["attempts"],
              _dur(d["rows"][n.id]["started_at"], d["rows"][n.id]["finished_at"])] for n in d["nodes"]],
        ),
        "<h2>Gates</h2>",
        _table(["Node", "Attempt", "Phase", "Gate", "Result", "Details"],
               [[g["node_id"], g["attempt"], g["phase"], g["gate"], "PASS" if g["passed"] else "FAIL", g["details"]]
                for g in d["gates"] if g["gate"] != "inputs_available"]),
        "<h2>Policy violations</h2>",
        _table(["Node", "Attempt", "Policy", "Outcome", "Evidence"],
               [[p["node_id"], p["attempt"], p["policy_id"], p["outcome"], "; ".join(p["evidence"])]
                for p in d["policies"] if p["outcome"] != "pass"]),
        "<h2>Approvals</h2>",
        _table(["Gate", "Risk", "Status", "Approver", "Auto", "Comment", "Reasons"],
               [[a.gate_id, a.risk.value, a.status.value, a.approver or "-", a.auto, a.comment or "-", "; ".join(a.reasons)]
                for a in d["approvals"]]),
        "<h2>Re-plans and notable events</h2>",
        _table(["Seq", "Event", "Details"], [[e["seq"], e["type"], json.dumps(e["payload"])] for e in d["notable"] + d["replans"]]),
        "<h2>Decisions</h2>",
        _table(["Decision", "Node", "Agent", "Parents", "Rationale"],
               [[x.decision_id, x.node_id, x.agent, ", ".join(x.parent_decision_ids), x.rationale] for x in d["decisions"]]),
        "<h2>Metrics</h2>",
        _table(["Metric", "Value"], [[k, json.dumps(v)] for k, v in m.items()]),
        "<h2>Artifacts</h2>",
        _table(["Key", "Version", "Producer", "Files"],
               [[a.key, a.version, a.node_id, ", ".join(a.paths)] for a in d["artifacts"] if not a.key.startswith("gates:")]),
        "</main></body></html>",
    ]  # fmt: skip
    return "\n".join(parts)
