"""SQLite-backed run state (``runs/<run_id>/state.db``).

The engine writes every transition here *before* acting on it, so a crashed or paused run
can be resumed from exactly where it stopped. All writes commit immediately; the database
is small and single-writer, so durability beats throughput here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.models import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    Decision,
    GateResult,
    LLMUsage,
    NodeState,
    PolicyResult,
    RunState,
    TaskNode,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    scenario TEXT,
    requirement_path TEXT NOT NULL,
    workspace TEXT NOT NULL,
    state TEXT NOT NULL,
    config_json TEXT NOT NULL,
    risk TEXT NOT NULL DEFAULT 'low',
    plan_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    executions INTEGER NOT NULL DEFAULT 0,
    checkpoint TEXT,
    pending_json TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    plan_version INTEGER NOT NULL DEFAULT 1,
    ordinal INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS node_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    reason TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    key TEXT NOT NULL,
    version INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, version)
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    body_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    gate_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    body_json TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    phase TEXT NOT NULL,
    body_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT,
    body_json TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT NOT NULL,
    node_id TEXT,
    start TEXT NOT NULL,
    end TEXT,
    status TEXT,
    attributes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""


def _ts(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


class StateStore:
    """Thin repository over one run's SQLite database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ run

    def create_run(
        self,
        run_id: str,
        *,
        scenario: str | None,
        requirement_path: str,
        workspace: str,
        config: dict[str, Any],
    ) -> None:
        now = _ts()
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs (run_id, scenario, requirement_path, workspace, state,"
                " config_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    scenario,
                    requirement_path,
                    workspace,
                    RunState.CREATED.value,
                    json.dumps(config),
                    now,
                    now,
                ),
            )

    def run(self) -> dict[str, Any]:
        rows = self._query("SELECT * FROM runs LIMIT 1")
        if not rows:
            raise LookupError(f"no run recorded in {self.path}")
        row = dict(rows[0])
        row["config"] = json.loads(row.pop("config_json"))
        return row

    def set_run_state(self, state: RunState) -> None:
        finished = _ts() if state in {RunState.SUCCEEDED, RunState.FAILED} else None
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET state=?, updated_at=?, finished_at=COALESCE(?, finished_at)",
                (state.value, _ts(), finished),
            )

    def set_run_fields(self, **fields: Any) -> None:
        allowed = {"risk", "plan_version", "requirement_path"}
        if not set(fields) <= allowed:
            raise ValueError(f"unknown run fields: {set(fields) - allowed}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            c.execute(
                f"UPDATE runs SET {assignments}, updated_at=?",  # noqa: S608  # nosec B608 - column names come from an allowlist
                (*fields.values(), _ts()),
            )

    # ------------------------------------------------------------------ nodes

    def upsert_node(self, node: TaskNode, *, plan_version: int) -> None:
        with self._tx() as c:
            existing = c.execute("SELECT ordinal FROM nodes WHERE node_id=?", (node.id,)).fetchone()
            if existing:
                c.execute(
                    "UPDATE nodes SET spec_json=?, plan_version=? WHERE node_id=?",
                    (node.model_dump_json(), plan_version, node.id),
                )
            else:
                ordinal = c.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM nodes").fetchone()[0]
                c.execute(
                    "INSERT INTO nodes (node_id, spec_json, state, plan_version, ordinal)"
                    " VALUES (?,?,?,?,?)",
                    (
                        node.id,
                        node.model_dump_json(),
                        NodeState.PENDING.value,
                        plan_version,
                        ordinal,
                    ),
                )

    def delete_node(self, node_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))

    def nodes(self) -> list[TaskNode]:
        return [
            TaskNode.model_validate_json(r["spec_json"])
            for r in self._query("SELECT spec_json FROM nodes ORDER BY ordinal")
        ]

    def node_row(self, node_id: str) -> dict[str, Any]:
        rows = self._query("SELECT * FROM nodes WHERE node_id=?", (node_id,))
        if not rows:
            raise KeyError(node_id)
        return dict(rows[0])

    def node_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM nodes ORDER BY ordinal")]

    def node_states(self) -> dict[str, NodeState]:
        return {
            r["node_id"]: NodeState(r["state"])
            for r in self._query("SELECT node_id, state FROM nodes ORDER BY ordinal")
        }

    def set_node_state(
        self, node_id: str, state: NodeState, *, attempt: int, reason: str = ""
    ) -> NodeState:
        with self._tx() as c:
            row = c.execute("SELECT state FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            if row is None:
                raise KeyError(node_id)
            previous = NodeState(row["state"])
            sets = ["state=?", "attempts=?"]
            params: list[Any] = [state.value, attempt]
            if state is NodeState.RUNNING:
                sets.append("started_at=COALESCE(started_at, ?)")
                params.append(_ts())
            if state in {NodeState.SUCCEEDED, NodeState.FAILED, NodeState.BLOCKED}:
                sets.append("finished_at=?")
                params.append(_ts())
            if state is NodeState.PENDING:
                sets.append("started_at=NULL, finished_at=NULL, error=NULL, pending_json=NULL")
            c.execute(
                f"UPDATE nodes SET {', '.join(sets)} WHERE node_id=?",  # noqa: S608  # nosec B608 - fixed column list
                (*params, node_id),
            )
            c.execute(
                "INSERT INTO node_events (node_id, from_state, to_state, attempt, reason, ts)"
                " VALUES (?,?,?,?,?,?)",
                (node_id, previous.value, state.value, attempt, reason, _ts()),
            )
            return previous

    def update_node(self, node_id: str, **fields: Any) -> None:
        allowed = {"checkpoint", "pending_json", "error", "executions", "attempts"}
        if not set(fields) <= allowed:
            raise ValueError(f"unknown node fields: {set(fields) - allowed}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as c:
            c.execute(
                f"UPDATE nodes SET {assignments} WHERE node_id=?",  # noqa: S608  # nosec B608 - column names come from an allowlist
                (*fields.values(), node_id),
            )

    def increment_executions(self, node_id: str) -> int:
        with self._tx() as c:
            c.execute("UPDATE nodes SET executions = executions + 1 WHERE node_id=?", (node_id,))
            return int(
                c.execute("SELECT executions FROM nodes WHERE node_id=?", (node_id,)).fetchone()[0]
            )

    def node_events(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM node_events ORDER BY id")]

    # ------------------------------------------------------------------ artifacts

    def put_artifact(self, artifact: Artifact) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?)",
                (
                    artifact.key,
                    artifact.version,
                    artifact.node_id,
                    json.dumps(artifact.content, default=str),
                    artifact.content_hash,
                    json.dumps(artifact.paths),
                    _ts(artifact.created_at),
                ),
            )

    def _artifact_from_row(self, r: sqlite3.Row) -> Artifact:
        return Artifact(
            key=r["key"],
            version=r["version"],
            node_id=r["node_id"],
            content=json.loads(r["content_json"]),
            content_hash=r["content_hash"],
            paths=json.loads(r["paths_json"]),
            created_at=datetime.fromisoformat(r["created_at"]),
        )

    def latest_artifact(self, key: str) -> Artifact | None:
        rows = self._query(
            "SELECT * FROM artifacts WHERE key=? ORDER BY version DESC LIMIT 1", (key,)
        )
        return self._artifact_from_row(rows[0]) if rows else None

    def artifacts(self, *, latest_only: bool = True) -> list[Artifact]:
        rows = self._query("SELECT * FROM artifacts ORDER BY created_at, key, version")
        items = [self._artifact_from_row(r) for r in rows]
        if not latest_only:
            return items
        latest: dict[str, Artifact] = {}
        for a in items:
            if a.key not in latest or a.version > latest[a.key].version:
                latest[a.key] = a
        return list(latest.values())

    # ------------------------------------------------------------------ decisions

    def put_decision(self, decision: Decision) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO decisions VALUES (?,?,?,?)",
                (
                    decision.decision_id,
                    decision.node_id,
                    decision.model_dump_json(),
                    _ts(decision.timestamp),
                ),
            )

    def decisions(self) -> list[Decision]:
        return [
            Decision.model_validate_json(r["body_json"])
            for r in self._query("SELECT body_json FROM decisions ORDER BY ts, rowid")
        ]

    # ------------------------------------------------------------------ approvals

    def put_approval(self, req: ApprovalRequest) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO approvals VALUES (?,?,?,?)",
                (req.gate_id, req.node_id, req.model_dump_json(), req.status.value),
            )

    def approval(self, gate_id: str) -> ApprovalRequest | None:
        rows = self._query("SELECT body_json FROM approvals WHERE gate_id=?", (gate_id,))
        return ApprovalRequest.model_validate_json(rows[0]["body_json"]) if rows else None

    def approvals(self, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        if status is None:
            rows = self._query("SELECT body_json FROM approvals ORDER BY rowid")
        else:
            rows = self._query(
                "SELECT body_json FROM approvals WHERE status=? ORDER BY rowid", (status.value,)
            )
        return [ApprovalRequest.model_validate_json(r["body_json"]) for r in rows]

    # ------------------------------------------------------------------ gates / policies / llm

    def add_gate_result(self, node_id: str, attempt: int, phase: str, result: GateResult) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO gate_results (node_id, attempt, phase, body_json, ts)"
                " VALUES (?,?,?,?,?)",
                (node_id, attempt, phase, result.model_dump_json(), _ts()),
            )

    def gate_results(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM gate_results ORDER BY id"):
            row = dict(r)
            row.update(json.loads(row.pop("body_json")))
            out.append(row)
        return out

    def add_policy_result(self, node_id: str, attempt: int, result: PolicyResult) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO policy_results (node_id, attempt, body_json, ts) VALUES (?,?,?,?)",
                (node_id, attempt, result.model_dump_json(), _ts()),
            )

    def policy_results(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM policy_results ORDER BY id"):
            row = dict(r)
            row.update(json.loads(row.pop("body_json")))
            out.append(row)
        return out

    def add_llm_call(self, node_id: str | None, usage: LLMUsage) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO llm_calls (node_id, body_json, ts) VALUES (?,?,?)",
                (node_id, usage.model_dump_json(), _ts()),
            )

    def llm_calls(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM llm_calls ORDER BY id"):
            row = dict(r)
            row.update(json.loads(row.pop("body_json")))
            out.append(row)
        return out

    # ------------------------------------------------------------------ spans

    def put_span(self, span: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?)",
                (
                    span["span_id"],
                    span.get("parent_id"),
                    span["name"],
                    span.get("node_id"),
                    span["start"],
                    span.get("end"),
                    span.get("status"),
                    json.dumps(span.get("attributes", {}), default=str),
                ),
            )

    def spans(self) -> list[dict[str, Any]]:
        out = []
        for r in self._query("SELECT * FROM spans ORDER BY start"):
            row = dict(r)
            row["attributes"] = json.loads(row.pop("attributes_json"))
            out.append(row)
        return out

    # ------------------------------------------------------------------ kv

    def kv_set(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def kv_get(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value_json FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value_json"]) if rows else default
