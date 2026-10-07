"""Reliability metrics, per run and across runs, exportable as JSON or Prometheus text.

Definitions (also in docs/OPERATIONS.md):

* node success rate  = succeeded / (succeeded + failed + blocked)
* retry rate         = retries / attempts  (retry = a RETRYING transition; attempt = RUNNING)
* reruns             = nodes re-executed because a re-plan invalidated them (not retries)
* rollback frequency = rollbacks / attempts of write nodes
* fallback frequency = LLM calls served by the fallback provider / all LLM calls
* MTTR               = mean time from an attempt failing to that node succeeding
* stage latency      = wall time per node type (first RUNNING -> SUCCEEDED)
* end-to-end latency = run created -> run finished (or last event when not finished)
* approval wait      = approval requested -> decided
"""

from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.models import NodeState, TaskNode
from orchestrator.state import StateStore

FAIL_TRANSITIONS = {
    (NodeState.RUNNING.value, NodeState.ROLLED_BACK.value),
    (NodeState.RUNNING.value, NodeState.RETRYING.value),
    (NodeState.RUNNING.value, NodeState.FAILED.value),
    (NodeState.AWAITING_APPROVAL.value, NodeState.ROLLED_BACK.value),
}


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _secs(a: str | None, b: str | None) -> float | None:
    da, db = _dt(a), _dt(b)
    return (db - da).total_seconds() if da and db else None


def _ratio(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def run_metrics(store: StateStore) -> dict[str, Any]:
    run = store.run()
    nodes = {n.id: n for n in store.nodes()}
    rows = {r["node_id"]: r for r in store.node_rows()}
    events = store.node_events()
    states = {nid: NodeState(r["state"]) for nid, r in rows.items()}

    counts = {s.value: sum(1 for v in states.values() if v is s) for s in NodeState}
    succeeded = counts["SUCCEEDED"]
    finished = succeeded + counts["FAILED"] + counts["BLOCKED"]

    attempts = [e for e in events if e["to_state"] == NodeState.RUNNING.value]
    attempts = [e for e in attempts if e["from_state"] != NodeState.AWAITING_APPROVAL.value]
    retries = sum(1 for e in events if e["to_state"] == NodeState.RETRYING.value)
    reruns = sum(
        1
        for e in events
        if e["to_state"] == NodeState.PENDING.value and "invalidated" in (e["reason"] or "")
    )
    write_attempts = [e for e in attempts if _is_write(nodes.get(e["node_id"]))]
    rollbacks = sum(1 for e in events if e["to_state"] == NodeState.ROLLED_BACK.value)

    # MTTR: failure event -> next success of the same node
    recoveries: list[float] = []
    unrecovered = 0
    by_node: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_node.setdefault(e["node_id"], []).append(e)
    for evs in by_node.values():
        open_failure: str | None = None
        for e in evs:
            if (e["from_state"], e["to_state"]) in FAIL_TRANSITIONS and open_failure is None:
                open_failure = e["ts"]
            elif e["to_state"] == NodeState.SUCCEEDED.value and open_failure is not None:
                gap = _secs(open_failure, e["ts"])
                if gap is not None:
                    recoveries.append(gap)
                open_failure = None
        if open_failure is not None:
            unrecovered += 1

    stage: dict[str, list[float]] = {}
    for nid, r in rows.items():
        if states[nid] is NodeState.SUCCEEDED and nid in nodes:
            d = _secs(r["started_at"], r["finished_at"])
            if d is not None:
                stage.setdefault(nodes[nid].type.value, []).append(d)

    approvals = store.approvals()
    waits = [
        w
        for a in approvals
        if (
            w := _secs(
                a.requested_at.isoformat(), a.decided_at.isoformat() if a.decided_at else None
            )
        )
        is not None
    ]
    llm = store.llm_calls()
    fallbacks = sum(1 for c in llm if c.get("fallback"))
    policies = store.policy_results()
    gates = store.gate_results()

    last_ts = events[-1]["ts"] if events else run["updated_at"]
    e2e = _secs(run["created_at"], run["finished_at"] or last_ts)

    return {
        "run_id": run["run_id"],
        "scenario": run["scenario"],
        "state": run["state"],
        "risk": run["risk"],
        "plan_version": run["plan_version"],
        "nodes": counts,
        "nodes_total": len(nodes),
        "node_success_rate": _ratio(succeeded, finished),
        "attempts": len(attempts),
        "retries": retries,
        "retry_rate": _ratio(retries, len(attempts)),
        "reruns_after_replan": reruns,
        "rollbacks": rollbacks,
        "rollback_frequency": _ratio(rollbacks, len(write_attempts)),
        "llm_calls": len(llm),
        "fallbacks": fallbacks,
        "fallback_frequency": _ratio(fallbacks, len(llm)),
        "llm_input_tokens": sum(int(c.get("input_tokens", 0)) for c in llm),
        "llm_output_tokens": sum(int(c.get("output_tokens", 0)) for c in llm),
        "llm_cost_usd": round(sum(float(c.get("cost_usd", 0.0)) for c in llm), 6),
        "mttr_s": round(statistics.mean(recoveries), 3) if recoveries else None,
        "recoveries": len(recoveries),
        "unrecovered_failures": unrecovered,
        "stage_latency_s": {k: round(sum(v), 3) for k, v in sorted(stage.items())},
        "end_to_end_latency_s": round(e2e, 3) if e2e is not None else None,
        "approvals": len(approvals),
        "approvals_auto": sum(1 for a in approvals if a.auto),
        "approvals_pending": sum(1 for a in approvals if a.status.value == "pending"),
        "approval_wait_mean_s": round(statistics.mean(waits), 3) if waits else None,
        "approval_wait_max_s": round(max(waits), 3) if waits else None,
        "gates_evaluated": len(gates),
        "gates_failed": sum(1 for g in gates if not g["passed"]),
        "policy_evaluations": len(policies),
        "policy_violations": {
            o: sum(1 for p in policies if p["outcome"] == o)
            for o in ("block", "require_approval", "warn")
        },
        "replans": int(store.kv_get("replans", 0)),
    }


def _is_write(node: TaskNode | None) -> bool:
    return bool(node and node.is_write)


def collect_runs(runs_dir: Path) -> list[dict[str, Any]]:
    out = []
    for db in sorted(runs_dir.glob("*/state.db")):
        store = StateStore(db)
        try:
            out.append(run_metrics(store))
        finally:
            store.close()
    return out


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    if not runs:
        return {"runs": 0}
    finished = [r for r in runs if r["state"] in {"SUCCEEDED", "FAILED"}]
    attempts = sum(r["attempts"] for r in runs)
    mttrs = [r["mttr_s"] for r in runs if r["mttr_s"] is not None]
    e2e = [r["end_to_end_latency_s"] for r in finished if r["end_to_end_latency_s"] is not None]
    llm = sum(r["llm_calls"] for r in runs)
    return {
        "runs": len(runs),
        "runs_succeeded": sum(1 for r in runs if r["state"] == "SUCCEEDED"),
        "run_success_rate": _ratio(
            sum(1 for r in finished if r["state"] == "SUCCEEDED"), len(finished)
        ),
        "node_success_rate": _ratio(
            sum(r["nodes"]["SUCCEEDED"] for r in runs),
            sum(
                r["nodes"]["SUCCEEDED"] + r["nodes"]["FAILED"] + r["nodes"]["BLOCKED"] for r in runs
            ),
        ),
        "retry_rate": _ratio(sum(r["retries"] for r in runs), attempts),
        "rollback_frequency": _ratio(sum(r["rollbacks"] for r in runs), attempts),
        "fallback_frequency": _ratio(sum(r["fallbacks"] for r in runs), llm),
        "mttr_s": round(statistics.mean(mttrs), 3) if mttrs else None,
        "end_to_end_latency_mean_s": round(statistics.mean(e2e), 3) if e2e else None,
        "approval_wait_mean_s": _mean([r["approval_wait_mean_s"] for r in runs]),
        "llm_tokens": sum(r["llm_input_tokens"] + r["llm_output_tokens"] for r in runs),
        "llm_cost_usd": round(sum(r["llm_cost_usd"] for r in runs), 6),
        "replans": sum(r["replans"] for r in runs),
    }


def _mean(values: list[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return round(statistics.mean(real), 3) if real else None


_PROM_HELP = {
    "node_success_rate": "Succeeded nodes / finished nodes",
    "retry_rate": "Retries / attempts",
    "rollback_frequency": "Rollbacks / write-node attempts",
    "fallback_frequency": "Fallback LLM calls / all LLM calls",
    "mttr_s": "Mean time to recover a failed node (seconds)",
    "end_to_end_latency_s": "Run wall time (seconds)",
    "approval_wait_mean_s": "Mean approval wait (seconds)",
    "retries": "Retries in the run",
    "rollbacks": "Rollbacks in the run",
    "llm_cost_usd": "LLM spend (USD)",
    "replans": "Dynamic re-plans",
}


def to_prometheus(runs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for key, help_text in _PROM_HELP.items():
        name = f"orchestrator_{key}"
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for r in runs:
            value = r.get(key)
            if value is None:
                continue
            lines.append(
                f'{name}{{run_id="{r["run_id"]}",scenario="{r["scenario"] or ""}",'
                f'state="{r["state"]}"}} {float(value)}'
            )
    lines.append("# HELP orchestrator_stage_latency_s Wall time per node type (seconds)")
    lines.append("# TYPE orchestrator_stage_latency_s gauge")
    for r in runs:
        for stage, secs in r.get("stage_latency_s", {}).items():
            lines.append(
                f'orchestrator_stage_latency_s{{run_id="{r["run_id"]}",stage="{stage}"}} {secs}'
            )
    agg = aggregate(runs)
    lines.append("# HELP orchestrator_run_success_rate Succeeded runs / finished runs")
    lines.append("# TYPE orchestrator_run_success_rate gauge")
    lines.append(f"orchestrator_run_success_rate {float(agg.get('run_success_rate', 0.0))}")
    return "\n".join(lines) + "\n"
