"""Kill a run mid-flight with SIGKILL, then resume it from persisted state."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from orchestrator.audit import verify_chain
from orchestrator.models import NodeState, RunState
from orchestrator.state import StateStore
from tests.orchestrator.fakes import FakeAgent, reopen

ROOT = Path(__file__).resolve().parents[2]


def _wait_for_running(db: Path, node_id: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db.exists():
            try:
                store = StateStore(db)
                states = store.node_states()
                store.close()
                if states.get(node_id) is NodeState.RUNNING:
                    return
            except Exception:  # noqa: S110 - db may be mid-creation; keep polling
                pass
        time.sleep(0.05)
    raise AssertionError(f"{node_id} never reached RUNNING")


async def test_sigkilled_run_resumes_without_redoing_completed_work(tmp_path: Path) -> None:
    proc = subprocess.Popen(  # noqa: S603 - test harness, fixed argv
        [sys.executable, "-m", "tests.orchestrator.crash_runner", str(tmp_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    db = tmp_path / "runs" / "run-test" / "state.db"
    try:
        _wait_for_running(db, "b")
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL

    store = StateStore(db)
    assert store.run()["state"] == RunState.RUNNING.value  # died without a clean shutdown
    assert store.node_states() == {
        "a": NodeState.SUCCEEDED,
        "b": NodeState.RUNNING,
        "c": NodeState.PENDING,
    }
    assert store.node_row("a")["executions"] == 1
    store.close()

    agent = FakeAgent()  # b no longer hangs
    orch = reopen(tmp_path, agent)
    assert await orch.resume() is RunState.SUCCEEDED
    assert [c.node_id for c in agent.calls] == ["b", "c"]  # a was NOT re-executed
    assert orch.store.node_row("a")["executions"] == 1
    assert orch.store.node_row("b")["executions"] == 2
    assert agent.calls[0].attempt == 2  # attempt counter survived the crash
    transitions = [
        (e["from_state"], e["to_state"]) for e in orch.store.node_events() if e["node_id"] == "b"
    ]
    assert ("RUNNING", "READY") in transitions  # crash recovery transition is recorded
    assert verify_chain(orch.run_dir / "audit.jsonl").ok
    assert "run_resumed" in [e["type"] for e in orch.audit.events()]
