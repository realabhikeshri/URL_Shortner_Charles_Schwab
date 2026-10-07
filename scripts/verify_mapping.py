"""Check that every code path and test named in docs/ENGINEERING_SUMMARY.md exists.

Backticked tokens are checked as file refs (contain ``/`` or end in a known extension) or as
``file::symbol`` refs (the symbol must be defined in the file; a trailing ``*`` matches a prefix).
Doc shorthand is supported: paths relative to the repo, ``tests/``, ``orchestrator/`` or a run
folder; ``::name`` continues the previous file in the same table row; ``{a,b}`` brace lists.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "ENGINEERING_SUMMARY.md"
TOKEN = re.compile(r"`([^`]+)`")
FILE_SUFFIXES = (
    ".py",
    ".yaml",
    ".yml",
    ".md",
    ".json",
    ".prom",
    ".mmd",
    ".html",
    ".jsonl",
    ".sh",
    ".toml",
)


def expand_braces(token: str) -> list[str]:
    m = re.search(r"\{([^{}]+)\}", token)
    if not m:
        return [token]
    return [
        r
        for alt in m.group(1).split(",")
        for r in expand_braces(token[: m.start()] + alt + token[m.end() :])
    ]


def resolve_file(rel: str) -> Path | None:
    """Resolve a doc path relative to the repo, tests/, orchestrator/ or a run folder."""
    for base in (ROOT, ROOT / "tests", ROOT / "orchestrator"):
        if "*" in rel:
            if any(base.glob(rel)):
                return base
        elif (base / rel).exists():
            return base / rel
    runs = ROOT / "runs"
    if runs.exists():
        for run in runs.glob("*-2*"):
            if run.is_dir() and (run / rel).exists():
                return run / rel
    return None


def check(token: str, last_file: Path | None) -> tuple[str | None, Path | None]:
    token = token.strip().rstrip(",;")
    if token.startswith(("make ", "orchestrator ", "uv ", "$")) or " " in token:
        return None, last_file
    if "::" in token:
        path, _, name = token.partition("::")
        file = resolve_file(path) if path else last_file
        if file is None or not file.is_file():
            return f"file not found: {path or '(no preceding file)'}", last_file
        text = file.read_text(encoding="utf-8")
        if name.endswith("*"):
            ok = re.search(rf"def {re.escape(name[:-1])}\w*", text)
        else:
            name = name.split(".")[-1]
            ok = re.search(rf"\b(def|class) {re.escape(name)}\b|^{re.escape(name)}\b", text, re.M)
        return (None if ok else f"{name} not in {file.name}"), file
    if "/" in token or token.endswith(FILE_SUFFIXES):
        if resolve_file(token.removeprefix("./")) is None:
            return f"path not found: {token}", last_file
        file = resolve_file(token)
        return None, file if file is not None and file.is_file() else last_file
    return None, last_file


def main() -> int:
    problems: list[str] = []
    checked = 0
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith("| #") or set(line) <= set("|- "):
            continue
        last: Path | None = None
        for cell in line.split(" | "):
            for raw in TOKEN.findall(cell):
                for tok in expand_braces(raw):
                    checked += 1
                    msg, last = check(tok, last)
                    if msg is not None:
                        problems.append(f"{tok!r}: {msg}")
    for p in problems:
        print(p)
    print(f"checked {checked} references; {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
