"""Lightweight OpenTelemetry-shaped tracing.

Spans follow the OTel data model (trace id = run id, span id, parent id, name, start/end,
status, attributes) and are persisted to the run's state DB and exported as JSONL, which
can be converted to OTLP later. We avoid the full OTel SDK to keep the engine dependency-light
(ADR-011).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.models import utcnow
from orchestrator.state import StateStore

_current_span: ContextVar[Span | None] = ContextVar("current_span", default=None)


@dataclass
class Span:
    trace_id: str
    name: str
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    parent_id: str | None = None
    node_id: str | None = None
    start: str = field(default_factory=lambda: utcnow().isoformat())
    end: str | None = None
    status: str = "UNSET"
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "node_id": self.node_id,
            "start": self.start,
            "end": self.end,
            "status": self.status,
            "attributes": self.attributes,
        }


class Tracer:
    def __init__(self, run_id: str, store: StateStore) -> None:
        self.run_id = run_id
        self.store = store

    @contextmanager
    def span(self, name: str, *, node_id: str | None = None, **attrs: Any) -> Iterator[Span]:
        parent = _current_span.get()
        span = Span(
            trace_id=self.run_id,
            name=name,
            parent_id=parent.span_id if parent else None,
            node_id=node_id or (parent.node_id if parent else None),
            attributes=dict(attrs),
        )
        token = _current_span.set(span)
        try:
            yield span
            if span.status == "UNSET":
                span.status = "OK"
        except BaseException as exc:
            span.status = "ERROR"
            span.attributes["error"] = repr(exc)[:500]
            raise
        finally:
            span.end = utcnow().isoformat()
            _current_span.reset(token)
            self.store.put_span(span.as_dict())

    def export_jsonl(self, path: Path) -> int:
        spans = self.store.spans()
        with path.open("w", encoding="utf-8") as fh:
            for s in spans:
                fh.write(json.dumps({"trace_id": self.run_id, **s}, sort_keys=True) + "\n")
        return len(spans)
