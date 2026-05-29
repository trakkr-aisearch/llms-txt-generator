"""URL hygiene, scoring, and the SSRF guard — all offline."""
import pytest

from llmstxt_generator.builder import (
    _is_asset,
    _norm_url,
    _same_site,
    _score_candidate,
    _select_candidates,
    _strip_locale,
)
from llmstxt_generator.fetchers import _safe_url, normalise_domain, root_url


def test_normalise_domain():
    assert normalise_domain("HTTPS://www.Stripe.com/pricing") == "stripe.com"
    assert normalise_domain("stripe.com/") == "stripe.com"
    assert normalise_domain("https://docs.stripe.com") == "docs.stripe.com"
    assert normalise_domain("") == ""


def test_root_url():
    assert root_url("stripe.com") == "https://stripe.com"
    assert root_url("https://stripe.com/pricing") == "https://stripe.com"


def test_same_site():
    assert _same_site("https://docs.stripe.com/x", "stripe.com")
    assert _same_site("https://www.stripe.com/x", "stripe.com")
    assert _same_site("https://stripe.com/x", "www.stripe.com")
    assert not _same_site("https://evil-stripe.com/x", "stripe.com")
    assert not _same_site("https://stripe.com.attacker.net/x", "stripe.com")


def test_strip_locale():
    assert _strip_locale("/gb/pricing") == "/pricing"
    assert _strip_locale("/en-us/docs") == "/docs"
    assert _strip_locale("/pricing") == "/pricing"
    assert _strip_locale("/products/gb") == "/products/gb"  # only leading segment


def test_norm_url_dedupes_consistently():
    a = _norm_url("https://Stripe.com/pricing/")
    b = _norm_url("https://stripe.com/gb/pricing")
    assert a == "https://stripe.com/pricing"
    assert b == "https://stripe.com/pricing"
    assert a == b


def test_is_asset():
    assert _is_asset("https://x.com/logo.png")
    assert _is_asset("https://x.com/feed.xml")
    assert not _is_asset("https://x.com/pricing")


def test_score_prefers_high_value_shallow_pages():
    pricing = _score_candidate("https://x.com/pricing")
    deep_blog = _score_candidate("https://x.com/blog/2021/01/some-post")
    legal = _score_candidate("https://x.com/legal/privacy")
    assert pricing > deep_blog
    assert pricing > legal


def test_select_candidates_filters_and_dedupes():
    home = [
        "https://x.com/pricing",
        "https://x.com/pricing/",          # dup after norm
        "https://x.com/logo.png",          # asset
        "https://other.com/spam",          # off-site
        "/relative/not-absolute",          # not absolute
    ]
    sitemap = ["https://x.com/docs", "https://x.com/"]  # root dropped
    out = _select_candidates("x.com", home, sitemap)
    assert "https://x.com/pricing" in out
    assert "https://x.com/docs" in out
    assert all("logo.png" not in u for u in out)
    assert all("other.com" not in u for u in out)
    # deduped
    assert out.count("https://x.com/pricing") == 1


# ── SSRF guard ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://localhost/x",
    "http://127.0.0.1/x",
    "http://10.0.0.1/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://metadata.google.internal/x",
    "http://[::1]/x",
    "ftp://example.com/x",
    "file:///etc/passwd",
])
def test_safe_url_blocks_dangerous(url):
    assert _safe_url(url) is not None  # returns an error string


def test_safe_url_allows_public():
    # 8.8.8.8 is a public IP and needs no DNS — should pass the guard.
    assert _safe_url("https://8.8.8.8/") is None
