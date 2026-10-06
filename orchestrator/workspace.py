"""Sandboxed workspace: the *only* way agents touch the target repository.

* Writes are confined to an allowlist of globs and can never hit the denylist
  (``policies/``, ``.github/``, ``orchestrator/``), regardless of what the agent asks for.
* Paths are resolved and must stay inside the workspace root (no ``..``/symlink escapes).
* Subprocesses run with an argv list (never ``shell=True``), a timeout and a scrubbed env.
* Git provides checkpoints; rollback is *path-scoped* so parallel nodes with disjoint write
  sets can roll back independently.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.hashing import sha256_text
from orchestrator.models import ChangeSet, FileOp

DEFAULT_WRITE_ALLOW = (
    "service/**",
    "tests/**",
    "docs/**",
    "scripts/**",
    "README.md",
    "CHANGELOG.md",
    "pyproject.toml",
)
DEFAULT_WRITE_DENY = ("policies/**", ".github/**", "orchestrator/**", ".git/**")
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "VIRTUAL_ENV")


class WorkspaceViolation(PermissionError):
    """An agent attempted an action outside its sandbox."""


class ApplyError(RuntimeError):
    """A change set could not be applied (e.g. a replace anchor was not found)."""


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, lines: int = 40) -> str:
        text = (self.stdout + ("\n" + self.stderr if self.stderr.strip() else "")).strip()
        return "\n".join(text.splitlines()[-lines:])


@dataclass
class AppliedChange:
    files: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    diff: str = ""
    lines_added: int = 0
    lines_removed: int = 0

    @property
    def diff_hash(self) -> str:
        return sha256_text(self.diff)

    @property
    def lines_changed(self) -> int:
        return self.lines_added + self.lines_removed

    def added_lines(self) -> list[tuple[str, str]]:
        """(path, line) for every added line in the diff."""
        out: list[tuple[str, str]] = []
        current = ""
        for line in self.diff.splitlines():
            if line.startswith("+++ "):
                current = line[6:] if line.startswith("+++ b/") else line[4:]
            elif line.startswith("+") and not line.startswith("+++"):
                out.append((current, line[1:]))
        return out


def match_any(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or path == p.rstrip("/*") for p in patterns)


class Workspace:
    def __init__(
        self,
        root: Path,
        *,
        allow: Sequence[str] = DEFAULT_WRITE_ALLOW,
        deny: Sequence[str] = DEFAULT_WRITE_DENY,
        python: str | None = None,
        default_timeout_s: float = 300.0,
    ) -> None:
        self.root = root.resolve()
        self.allow = tuple(allow)
        self.deny = tuple(deny)
        self.python = python or shutil.which("python") or "python3"
        self.default_timeout_s = default_timeout_s
        self._git_lock = asyncio.Lock()

    # ------------------------------------------------------------------ paths

    def resolve(self, rel: str) -> Path:
        if os.path.isabs(rel):
            raise WorkspaceViolation(f"absolute paths are not allowed: {rel}")
        target = (self.root / rel).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkspaceViolation(f"path escapes workspace: {rel}")
        return target

    def check_writable(self, rel: str) -> None:
        norm = Path(rel).as_posix()
        self.resolve(norm)
        if match_any(norm, self.deny):
            raise WorkspaceViolation(f"write denied by change-control denylist: {norm}")
        if not match_any(norm, self.allow):
            raise WorkspaceViolation(f"write outside allowlist: {norm}")

    def read(self, rel: str) -> str:
        return self.resolve(rel).read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        return self.resolve(rel).exists()

    def list_files(self, pattern: str = "**/*.py") -> list[str]:
        out = []
        for p in sorted(self.root.glob(pattern)):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and not rel.startswith((".git/", ".venv/")) and "__pycache__" not in rel:
                out.append(rel)
        return out

    # ------------------------------------------------------------------ writes

    def apply(self, changeset: ChangeSet, *, allowed_paths: Sequence[str] = ()) -> AppliedChange:
        """Apply all ops atomically: validate everything first, then write."""
        planned: dict[str, str | None] = {}
        before: dict[str, str | None] = {}
        for op in changeset.ops:
            norm = Path(op.path).as_posix()
            self.check_writable(norm)
            if allowed_paths and not match_any(norm, allowed_paths):
                raise WorkspaceViolation(f"{norm} is outside this node's declared write_paths")
            if norm not in before:
                path = self.resolve(norm)
                before[norm] = path.read_text(encoding="utf-8") if path.exists() else None
            current = planned.get(norm, before[norm])
            planned[norm] = self._apply_op(op, norm, current)

        applied = AppliedChange()
        diffs: list[str] = []
        for rel, new in planned.items():
            old = before[rel]
            if old == new:
                continue
            path = self.resolve(rel)
            if new is None:
                path.unlink(missing_ok=True)
                applied.deleted.append(rel)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new, encoding="utf-8")
                if old is None:
                    applied.created.append(rel)
            applied.files.append(rel)
            diff = list(
                difflib.unified_diff(
                    (old or "").splitlines(keepends=True),
                    (new or "").splitlines(keepends=True),
                    fromfile=f"a/{rel}" if old is not None else "/dev/null",
                    tofile=f"b/{rel}" if new is not None else "/dev/null",
                )
            )
            for line in diff:
                if line.startswith("+") and not line.startswith("+++"):
                    applied.lines_added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    applied.lines_removed += 1
            diffs.append("".join(diff))
        applied.diff = "\n".join(diffs)
        return applied

    @staticmethod
    def _apply_op(op: FileOp, rel: str, current: str | None) -> str | None:
        if op.op == "write":
            if op.content is None:
                raise ApplyError(f"write {rel}: missing content")
            return op.content
        if op.op == "delete":
            if current is None:
                raise ApplyError(f"delete {rel}: file does not exist")
            return None
        if current is None:
            raise ApplyError(f"replace {rel}: file does not exist")
        if op.old is None or op.new is None:
            raise ApplyError(f"replace {rel}: old/new required")
        count = current.count(op.old)
        if count != 1:
            raise ApplyError(
                f"replace {rel}: anchor found {count} times (must be exactly once): "
                f"{op.old.strip()[:80]!r}"
            )
        return current.replace(op.old, op.new)

    # ------------------------------------------------------------------ processes

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
        env["PYTHONPATH"] = str(self.root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra or {})
        return env

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if isinstance(argv, str):
            raise WorkspaceViolation("commands must be argv lists, never shell strings")
        args = [str(a) for a in argv]
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.root,
            env=self.env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s or self.default_timeout_s
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return CommandResult(args, -9, "", "timed out", time.perf_counter() - started, True)
        return CommandResult(
            args,
            proc.returncode if proc.returncode is not None else -1,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
            time.perf_counter() - started,
        )

    async def run_python(self, *args: str, timeout_s: float | None = None) -> CommandResult:
        return await self.run([self.python, *args], timeout_s=timeout_s)

    # ------------------------------------------------------------------ git

    async def _git(self, *args: str) -> CommandResult:
        res = await self.run(
            ["git", "-c", "user.name=orchestrator", "-c", "user.email=orchestrator@local", *args],
            timeout_s=60,
        )
        if not res.ok:
            raise RuntimeError(f"git {' '.join(args)} failed: {res.tail(10)}")
        return res

    async def init_git(self) -> None:
        if not (self.root / ".git").exists():
            await self._git("init", "-q", "-b", "main")
        async with self._git_lock:
            await self._git("add", "-A")
            status = await self._git("status", "--porcelain")
            if status.stdout.strip() or not await self._has_head():
                await self._git("commit", "-q", "--allow-empty", "-m", "workspace baseline")

    async def _has_head(self) -> bool:
        res = await self.run(["git", "rev-parse", "--verify", "HEAD"], timeout_s=30)
        return res.ok

    async def head(self) -> str:
        return (await self._git("rev-parse", "HEAD")).stdout.strip()

    async def checkpoint(self, label: str) -> str:
        """Commit any pending changes (e.g. human edits) and return HEAD as the checkpoint."""
        async with self._git_lock:
            status = await self._git("status", "--porcelain")
            if status.stdout.strip():
                await self._git("add", "-A")
                await self._git("commit", "-q", "-m", f"checkpoint: {label}")
            return await self.head()

    async def commit(self, paths: Sequence[str], message: str) -> str | None:
        async with self._git_lock:
            if not paths:
                return None
            await self._git("add", "-A", "--", *paths)
            staged = await self.run(["git", "diff", "--cached", "--quiet"], timeout_s=30)
            if staged.returncode == 0:
                return None
            await self._git("commit", "-q", "-m", message)
            return await self.head()

    async def rollback(self, checkpoint: str, paths: Sequence[str]) -> list[str]:
        """Restore ``paths`` to their state at ``checkpoint`` (path-scoped, parallel-safe)."""
        restored: list[str] = []
        async with self._git_lock:
            for rel in paths:
                existed = await self.run(
                    ["git", "cat-file", "-e", f"{checkpoint}:{rel}"], timeout_s=30
                )
                target = self.resolve(rel)
                if existed.ok:
                    await self._git("checkout", checkpoint, "--", rel)
                    await self._git("reset", "-q", "--", rel)
                elif target.exists():
                    target.unlink()
                    await self.run(["git", "rm", "-q", "--cached", "--", rel], timeout_s=30)
                restored.append(rel)
        return restored

    async def diff_since(self, rev: str, paths: Sequence[str] = ()) -> str:
        res = await self._git("diff", rev, "--", *paths)
        return res.stdout

    async def blame_commit(self, rel: str, line: int) -> str | None:
        res = await self.run(
            ["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", rel], timeout_s=30
        )
        if not res.ok or not res.stdout:
            return None
        return res.stdout.split()[0]

    async def commit_message(self, sha: str) -> str:
        return (await self._git("log", "-1", "--format=%B", sha)).stdout.strip()


class ToolBelt:
    """Per-agent view of the workspace that enforces the agent's declared permissions."""

    def __init__(self, workspace: Workspace, *, agent: str, permissions: frozenset[str]) -> None:
        self.workspace = workspace
        self.agent = agent
        self.permissions = permissions

    def _require(self, perm: str) -> None:
        if perm not in self.permissions:
            raise WorkspaceViolation(f"agent {self.agent} lacks '{perm}' permission")

    @property
    def root(self) -> Path:
        self._require("read")
        return self.workspace.root

    def read(self, rel: str) -> str:
        self._require("read")
        return self.workspace.read(rel)

    def exists(self, rel: str) -> bool:
        self._require("read")
        return self.workspace.exists(rel)

    def list_files(self, pattern: str = "**/*.py") -> list[str]:
        self._require("read")
        return self.workspace.list_files(pattern)

    async def run(self, argv: Sequence[str], *, timeout_s: float | None = None) -> CommandResult:
        self._require("run")
        return await self.workspace.run(argv, timeout_s=timeout_s)

    async def run_python(self, *args: str, timeout_s: float | None = None) -> CommandResult:
        self._require("run")
        return await self.workspace.run_python(*args, timeout_s=timeout_s)
