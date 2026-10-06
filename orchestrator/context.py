"""Cross-stage context: typed, content-hashed artifacts plus decision lineage."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from orchestrator.hashing import sha256_file, sha256_json
from orchestrator.models import Artifact, Decision
from orchestrator.state import StateStore

M = TypeVar("M", bound=BaseModel)


class ContextStore:
    """Versioned artifact registry shared by every node of a run.

    Artifacts are immutable versions; publishing a key again creates version n+1. Each
    artifact records the sha256 of its content and of every workspace file it claims, which
    is what dynamic re-planning compares against to detect upstream changes.
    """

    def __init__(self, store: StateStore, workspace_root: Path | None = None) -> None:
        self.store = store
        self.workspace_root = workspace_root

    def publish(
        self, key: str, content: Any, *, node_id: str, paths: list[str] | None = None
    ) -> Artifact:
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="json")
        previous = self.store.latest_artifact(key)
        file_hashes: dict[str, str | None] = {}
        for p in paths or []:
            file_hashes[p] = sha256_file(self.workspace_root / p) if self.workspace_root else None
        artifact = Artifact(
            key=key,
            version=(previous.version + 1) if previous else 1,
            node_id=node_id,
            content=content,
            content_hash=sha256_json(content),
            paths=file_hashes,
        )
        self.store.put_artifact(artifact)
        return artifact

    def get(self, key: str) -> Artifact | None:
        return self.store.latest_artifact(key)

    def content(self, key: str, default: Any = None) -> Any:
        art = self.get(key)
        return art.content if art else default

    def typed(self, key: str, model: type[M]) -> M | None:
        art = self.get(key)
        return model.model_validate(art.content) if art else None

    def has(self, key: str) -> bool:
        return self.get(key) is not None


class DecisionLog:
    """Decision records with parent links, so any output can be traced to the requirement."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def record(
        self,
        *,
        node_id: str,
        agent: str,
        inputs: Any,
        rationale: str,
        alternatives: list[str] | None = None,
        parents: list[str] | None = None,
        outcome: str = "",
    ) -> Decision:
        decision = Decision(
            decision_id=f"D-{secrets.token_hex(5)}",
            node_id=node_id,
            agent=agent,
            inputs_hash=sha256_json(inputs),
            rationale=rationale,
            alternatives_considered=alternatives or [],
            parent_decision_ids=parents or [],
            outcome=outcome,
        )
        self.store.put_decision(decision)
        return decision

    def all(self) -> list[Decision]:
        return self.store.decisions()

    def latest_for_nodes(self, node_ids: list[str]) -> list[str]:
        latest: dict[str, str] = {}
        for d in self.store.decisions():
            if d.node_id in node_ids:
                latest[d.node_id] = d.decision_id
        return [latest[n] for n in node_ids if n in latest]

    def lineage(self, decision_id: str) -> list[Decision]:
        """Walk parent links back to the root(s); returns decisions closest-first."""
        by_id = {d.decision_id: d for d in self.store.decisions()}
        seen: set[str] = set()
        order: list[Decision] = []
        frontier = [decision_id]
        while frontier:
            current = frontier.pop(0)
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            order.append(by_id[current])
            frontier.extend(by_id[current].parent_decision_ids)
        return order
