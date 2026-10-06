"""Append-only, hash-chained audit log (one JSONL file per run).

Each event carries ``prev_hash`` (the previous event's hash) and ``hash`` =
sha256(canonical JSON of the event without ``hash``). Editing, inserting, deleting or
re-ordering any line breaks the chain, which :func:`verify_chain` detects.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.hashing import canonical_json, sha256_text
from orchestrator.models import utcnow

GENESIS_HASH = "0" * 64


def _event_hash(event: dict[str, Any]) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return sha256_text(canonical_json(body))


class AuditLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._last_hash = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS_HASH
        last: dict[str, Any] | None = None
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)
        if last is None:
            return 0, GENESIS_HASH
        return int(last["seq"]), str(last["hash"])

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        with self._lock:
            event: dict[str, Any] = {
                "seq": self._seq + 1,
                "ts": utcnow().isoformat(),
                "run_id": self.run_id,
                "type": event_type,
                "payload": json.loads(canonical_json(payload)),
                "prev_hash": self._last_hash,
            }
            event["hash"] = _event_hash(event)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(canonical_json(event) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._seq = event["seq"]
            self._last_hash = event["hash"]
            return event

    def events(self) -> list[dict[str, Any]]:
        return read_events(self.path)


def read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    events: int
    error: str | None = None
    bad_seq: int | None = None


def verify_chain(path: Path) -> ChainVerification:
    prev = GENESIS_HASH
    count = 0
    try:
        events = read_events(path)
    except json.JSONDecodeError as exc:
        return ChainVerification(False, 0, f"unparseable line: {exc}")
    for expected_seq, event in enumerate(events, start=1):
        count += 1
        seq = event.get("seq")
        if seq != expected_seq:
            return ChainVerification(False, count, f"sequence gap: expected {expected_seq}", seq)
        if event.get("prev_hash") != prev:
            return ChainVerification(False, count, "prev_hash does not match previous event", seq)
        if _event_hash(event) != event.get("hash"):
            return ChainVerification(False, count, "event hash mismatch (content altered)", seq)
        prev = event["hash"]
    return ChainVerification(True, count)
