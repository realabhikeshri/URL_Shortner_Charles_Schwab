# lineage: REQ-GF-001 / implement.codes (see runs/ for the decision trail)
import pytest

from service.domain.codes import (
    ALPHABET,
    AliasError,
    collision_probability,
    decode,
    encode,
    generate_code,
    normalize_alias,
)


@pytest.mark.parametrize(("number", "code"), [(0, "0"), (61, "z"), (62, "10"), (3843, "zz")])
def test_encode_decode_known_values(number: int, code: str) -> None:
    assert encode(number) == code
    assert decode(code) == number


def test_encode_and_decode_reject_bad_input() -> None:
    with pytest.raises(ValueError, match="negative"):
        encode(-1)
    with pytest.raises(ValueError, match="empty"):
        decode("")
    with pytest.raises(ValueError, match="invalid base62"):
        decode("ab!")


def test_generated_codes_have_exact_length_and_alphabet() -> None:
    codes = {generate_code(7) for _ in range(500)}
    assert all(len(c) == 7 and set(c) <= set(ALPHABET) for c in codes)
    assert len(codes) == 500  # 62^7 space: duplicates in 500 draws would be astonishing


def test_collision_probability_matches_capacity_math() -> None:
    assert collision_probability(0) == 0.0
    assert collision_probability(1_000_000_000) == pytest.approx(2.84e-4, rel=0.01)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("launch-2026", "launch-2026"), ("My_Link", "my_link"), ("  abc  ", "abc")],
)
def test_valid_aliases_are_canonicalised(alias: str, canonical: str) -> None:
    assert normalize_alias(alias) == canonical


@pytest.mark.parametrize("alias", ["ab", "-start", "has space", "x" * 33, "ünïcode", "a/b"])
def test_invalid_aliases_are_rejected(alias: str) -> None:
    with pytest.raises(AliasError):
        normalize_alias(alias)


@pytest.mark.parametrize("alias", ["api", "healthz", "METRICS", "Docs"])
def test_reserved_aliases_are_rejected(alias: str) -> None:
    with pytest.raises(AliasError, match="reserved"):
        normalize_alias(alias)
