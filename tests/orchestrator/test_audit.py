import json
from pathlib import Path

from orchestrator.audit import AuditLog, verify_chain


def _log(tmp_path: Path, n: int = 5) -> Path:
    log = AuditLog(tmp_path / "audit.jsonl", "r1")
    for i in range(n):
        log.append("event", index=i, detail={"nested": [i, i + 1]})
    return log.path


def test_intact_chain_verifies(tmp_path: Path) -> None:
    path = _log(tmp_path)
    result = verify_chain(path)
    assert result.ok
    assert result.events == 5


def test_reopening_continues_the_chain(tmp_path: Path) -> None:
    path = _log(tmp_path, 3)
    AuditLog(path, "r1").append("later")
    events = [json.loads(x) for x in path.read_text().splitlines()]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert verify_chain(path).ok


def test_edited_event_is_detected(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = path.read_text().splitlines()
    event = json.loads(lines[2])
    event["payload"]["index"] = 99
    lines[2] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n")
    result = verify_chain(path)
    assert not result.ok
    assert result.bad_seq == 3
    assert "altered" in (result.error or "")


def test_deleted_and_reordered_events_are_detected(tmp_path: Path) -> None:
    path = _log(tmp_path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    assert not verify_chain(path).ok

    path2 = _log(tmp_path / "b")
    lines = path2.read_text().splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path2.write_text("\n".join(lines) + "\n")
    assert not verify_chain(path2).ok


def test_rehashed_forgery_still_breaks_the_link(tmp_path: Path) -> None:
    """An attacker who recomputes an edited event's own hash still breaks prev_hash links."""
    from orchestrator.audit import _event_hash

    path = _log(tmp_path)
    lines = path.read_text().splitlines()
    event = json.loads(lines[1])
    event["payload"]["index"] = 42
    event["hash"] = _event_hash(event)
    lines[1] = json.dumps(event)
    path.write_text("\n".join(lines) + "\n")
    result = verify_chain(path)
    assert not result.ok
    assert result.bad_seq == 3


def test_garbage_line_fails_verification(tmp_path: Path) -> None:
    path = _log(tmp_path)
    with path.open("a") as fh:
        fh.write("{not json\n")
    assert not verify_chain(path).ok
