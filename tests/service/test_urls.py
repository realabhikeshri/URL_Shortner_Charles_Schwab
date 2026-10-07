# lineage: REQ-GF-001 / implement.urls (see runs/ for the decision trail)
import pytest

from service.domain.urls import UrlError, normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.COM", "https://example.com/"),
        ("HTTP://example.com:80/a?b=1#c", "http://example.com/a?b=1#c"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("  https://example.com/path  ", "https://example.com/path"),
        ("https://[2606:4700::1111]/", "https://[2606:4700::1111]/"),
    ],
)
def test_valid_urls_are_normalised(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "example.com",
        "https://",
        "https://user:pass@example.com/",
        "https://example.com:99999/",
        "https://exa mple.com/",
        "https://example.com/\nheader",
        "",
    ],
)
def test_unsafe_or_malformed_urls_are_rejected(raw: str) -> None:
    with pytest.raises(UrlError):
        normalize_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://localhost:8000/",
        "http://printer.local/",
        "http://0.0.0.0/",
    ],
)
def test_private_and_local_targets_are_rejected_by_default(raw: str) -> None:
    with pytest.raises(UrlError):
        normalize_url(raw)
    assert normalize_url(raw, allow_private=True).startswith("http://")


def test_length_limit() -> None:
    url = "https://example.com/" + "a" * 2100
    with pytest.raises(UrlError, match="exceeds"):
        normalize_url(url)
    assert normalize_url(url, max_length=4096) == url
