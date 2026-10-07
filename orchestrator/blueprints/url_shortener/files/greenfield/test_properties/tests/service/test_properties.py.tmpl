# lineage: REQ-GF-001 / test.properties (see runs/ for the decision trail)
"""Property-based tests (hypothesis) for code generation and URL validation."""

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from service.domain.codes import ALPHABET, AliasError, decode, encode, normalize_alias
from service.domain.urls import UrlError, normalize_url

hosts = st.from_regex(r"[a-z][a-z0-9-]{0,20}\.(com|org|io|example)", fullmatch=True)
paths = st.lists(
    st.text(alphabet=string.ascii_letters + string.digits + "-_.~", max_size=12), max_size=4
)


@given(st.integers(min_value=0, max_value=2**128))
def test_base62_roundtrip(number: int) -> None:
    assert decode(encode(number)) == number


@given(
    st.text(alphabet=ALPHABET, min_size=1, max_size=20).filter(lambda s: s[0] != "0" or s == "0")
)
def test_base62_decode_encode_roundtrip_without_leading_zeros(code: str) -> None:
    assert encode(decode(code)) == code


@given(st.integers(min_value=0, max_value=2**64), st.integers(min_value=0, max_value=2**64))
def test_base62_preserves_order_for_equal_length_codes(a: int, b: int) -> None:
    ea, eb = encode(a), encode(b)
    if len(ea) == len(eb):
        assert (ea < eb) == (a < b)


@settings(max_examples=200)
@given(st.sampled_from(["http", "https", "HTTP", "Https"]), hosts, paths)
def test_normalisation_is_idempotent_and_safe(scheme: str, host: str, segments: list[str]) -> None:
    url = f"{scheme}://{host}/" + "/".join(segments)
    once = normalize_url(url)
    assert normalize_url(once) == once
    assert once.startswith(("http://", "https://"))
    assert once.split("://", 1)[1].split("/", 1)[0] == host


@given(st.text(max_size=200))
def test_validator_never_accepts_non_http_schemes(text: str) -> None:
    try:
        result = normalize_url(text)
    except UrlError:
        return
    assert result.startswith(("http://", "https://"))


@given(st.sampled_from(["javascript", "data", "file", "vbscript", "ftp"]), st.text(max_size=50))
def test_dangerous_schemes_always_rejected(scheme: str, rest: str) -> None:
    try:
        normalize_url(f"{scheme}:{rest}")
    except UrlError:
        return
    raise AssertionError("dangerous scheme accepted")


@given(st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_-]{2,31}", fullmatch=True))
def test_alias_canonical_form_is_stable(alias: str) -> None:
    try:
        canonical = normalize_alias(alias)
    except AliasError:
        return  # reserved word
    assert canonical == alias.lower()
    assert normalize_alias(canonical) == canonical
