"""Curated domain knowledge used by the deterministic provider (URL-shortener domain).

* ``knowledge.yaml`` - analyst rules (vague terms, capability keywords, open questions)
* ``catalog.yaml``   - capabilities -> typed plan nodes, design notes, acceptance criteria
* ``opsets/``        - per-node change-set manifests (metadata, replace ops, fault variants)
* ``files/``         - file templates written by the opsets (``<path>.tmpl``)
* ``scaffold/``      - the empty workspace a greenfield run starts from
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from orchestrator.models import FileOp

ROOT = Path(__file__).resolve().parent / "url_shortener"


@dataclass
class OpSet:
    name: str
    summary: str
    rationale: str
    alternatives: list[str]
    ops: list[FileOp]
    first_attempt: str | None = None
    repair_signatures: list[str] = field(default_factory=list)


@dataclass
class Blueprint:
    root: Path = ROOT

    @property
    def knowledge(self) -> dict[str, Any]:
        return _load_yaml(self.root / "knowledge.yaml")

    @property
    def catalog(self) -> dict[str, Any]:
        return _load_yaml(self.root / "catalog.yaml")

    @property
    def scaffold_dir(self) -> Path:
        return self.root / "scaffold"

    def has_opset(self, name: str) -> bool:
        return (self.root / "opsets" / f"{name}.yaml").is_file()

    def opset(self, name: str) -> OpSet:
        manifest = _load_yaml(self.root / "opsets" / f"{name}.yaml")
        ops: list[FileOp] = []
        files_dir = self.root / "files" / name
        if files_dir.is_dir():
            for tmpl in sorted(files_dir.rglob("*.tmpl")):
                rel = tmpl.relative_to(files_dir).as_posix()[: -len(".tmpl")]
                ops.append(FileOp(op="write", path=rel, content=tmpl.read_text(encoding="utf-8")))
        for raw in manifest.get("ops") or []:
            ops.append(FileOp.model_validate(raw))
        return OpSet(
            name=name,
            summary=manifest["summary"],
            rationale=" ".join(str(manifest.get("rationale", "")).split()),
            alternatives=list(manifest.get("alternatives") or []),
            ops=ops,
            first_attempt=manifest.get("first_attempt"),
            repair_signatures=list(manifest.get("repair_signatures") or []),
        )


@cache
def _load_yaml_cached(path: str, mtime: float) -> Any:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict[str, Any]:
    data = _load_yaml_cached(str(path), path.stat().st_mtime)
    return dict(data or {})
