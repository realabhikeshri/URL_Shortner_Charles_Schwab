from pathlib import Path

from orchestrator.hashing import canonical_json, sha256_file, sha256_json, sha256_text


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})
    assert sha256_json({"b": 1, "a": 2}) == sha256_json({"a": 2, "b": 1})


def test_sha256_text_matches_known_vector() -> None:
    assert sha256_text("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_file_handles_missing_and_present(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    assert sha256_file(target) is None
    target.write_text("abc")
    assert sha256_file(target) == sha256_text("abc")
