import asyncio
import sys
from pathlib import Path

import pytest

from orchestrator.models import ChangeSet, FileOp
from orchestrator.workspace import ApplyError, ToolBelt, Workspace, WorkspaceViolation


def ws_at(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    (root / "service").mkdir(parents=True)
    (root / "service" / "app.py").write_text("A = 1\nB = 2\n")
    return Workspace(root, python=sys.executable)


def cs(*ops: FileOp) -> ChangeSet:
    return ChangeSet(summary="t", ops=list(ops))


def test_path_escape_and_denylist_and_allowlist(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    with pytest.raises(WorkspaceViolation, match="escapes"):
        ws.check_writable("../outside.py")
    with pytest.raises(WorkspaceViolation, match="absolute"):
        ws.resolve("/etc/passwd")
    with pytest.raises(WorkspaceViolation, match="denylist"):
        ws.check_writable("policies/security.yaml")
    with pytest.raises(WorkspaceViolation, match="denylist"):
        ws.check_writable("orchestrator/engine.py")
    with pytest.raises(WorkspaceViolation, match="allowlist"):
        ws.check_writable("setup.cfg")
    ws.check_writable("service/new.py")


def test_symlink_escape_is_blocked(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws.root / "service" / "link").symlink_to(outside)
    with pytest.raises(WorkspaceViolation, match="escapes"):
        ws.check_writable("service/link/evil.py")


def test_apply_write_replace_delete_and_diff_stats(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    applied = ws.apply(
        cs(
            FileOp(op="replace", path="service/app.py", old="B = 2\n", new="B = 3\nC = 4\n"),
            FileOp(op="write", path="service/new.py", content="X = 1\n"),
        )
    )
    assert ws.read("service/app.py") == "A = 1\nB = 3\nC = 4\n"
    assert applied.created == ["service/new.py"]
    assert sorted(applied.files) == ["service/app.py", "service/new.py"]
    assert (applied.lines_added, applied.lines_removed) == (3, 1)
    assert ("service/new.py", "X = 1") in applied.added_lines()
    assert len(applied.diff_hash) == 64
    deleted = ws.apply(cs(FileOp(op="delete", path="service/new.py")))
    assert deleted.deleted == ["service/new.py"]
    assert not ws.exists("service/new.py")


def test_apply_is_all_or_nothing(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    with pytest.raises(ApplyError, match="anchor found 0 times"):
        ws.apply(
            cs(
                FileOp(op="write", path="service/new.py", content="X = 1\n"),
                FileOp(op="replace", path="service/app.py", old="NOPE", new="x"),
            )
        )
    assert not ws.exists("service/new.py")  # the valid first op was not written
    with pytest.raises(ApplyError, match="does not exist"):
        ws.apply(cs(FileOp(op="delete", path="service/ghost.py")))
    with pytest.raises(ApplyError, match="missing content"):
        ws.apply(cs(FileOp(op="write", path="service/x.py")))
    with pytest.raises(WorkspaceViolation, match="declared write_paths"):
        ws.apply(
            cs(FileOp(op="write", path="service/x.py", content="")),
            allowed_paths=["tests/**"],
        )


async def test_run_uses_argv_with_timeout_and_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "x")
    ws = ws_at(tmp_path)
    res = await ws.run_python("-c", "import os; print(os.environ.get('SUPER_SECRET_TOKEN'))")
    assert res.ok
    assert res.stdout.strip() == "None"  # secrets are not leaked into agent subprocesses
    with pytest.raises(WorkspaceViolation, match="argv"):
        await ws.run("echo hi")  # type: ignore[arg-type]
    slow = await ws.run_python("-c", "import time; time.sleep(5)", timeout_s=0.3)
    assert slow.timed_out
    assert not slow.ok


async def test_git_checkpoint_commit_and_path_scoped_rollback(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    await ws.init_git()
    base = await ws.checkpoint("start")
    # two "parallel" nodes write disjoint files
    a = ws.apply(cs(FileOp(op="replace", path="service/app.py", old="A = 1", new="A = 100")))
    b = ws.apply(cs(FileOp(op="write", path="tests/test_new.py", content="def test(): ...\n")))
    # node A fails -> roll back only A's paths; B's in-flight work survives
    restored = await ws.rollback(base, a.files)
    assert restored == ["service/app.py"]
    assert ws.read("service/app.py").startswith("A = 1")
    assert ws.exists("tests/test_new.py")
    # rolling back a file that did not exist at the checkpoint deletes it
    await ws.rollback(base, b.files)
    assert not ws.exists("tests/test_new.py")

    ws.apply(cs(FileOp(op="write", path="service/x.py", content="Y = 2\n")))
    sha = await ws.commit(["service/x.py"], "node x: add Y\n\ndecision: D-123")
    assert sha is not None
    assert await ws.commit(["service/x.py"], "nothing staged") is None
    assert "D-123" in await ws.commit_message(sha)
    assert await ws.blame_commit("service/x.py", 1) == sha
    assert "+Y = 2" in await ws.diff_since(base)


async def test_concurrent_checkpoints_are_serialised(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    await ws.init_git()
    for i in range(4):
        (ws.root / "service" / f"f{i}.py").write_text(f"V = {i}\n")
    shas = await asyncio.gather(*(ws.checkpoint(f"c{i}") for i in range(4)))
    assert len(set(shas)) >= 1  # no git index.lock errors under concurrency


def test_toolbelt_enforces_permissions(tmp_path: Path) -> None:
    ws = ws_at(tmp_path)
    reader = ToolBelt(ws, agent="Reviewer", permissions=frozenset({"read"}))
    assert "A = 1" in reader.read("service/app.py")
    assert reader.exists("service/app.py")
    assert reader.list_files() == ["service/app.py"]
    assert reader.root == ws.root
    with pytest.raises(WorkspaceViolation, match="lacks 'run'"):
        asyncio.run(reader.run(["true"]))
    with pytest.raises(WorkspaceViolation, match="lacks 'run'"):
        asyncio.run(reader.run_python("-c", "1"))
    blind = ToolBelt(ws, agent="X", permissions=frozenset())
    with pytest.raises(WorkspaceViolation, match="lacks 'read'"):
        blind.read("service/app.py")
