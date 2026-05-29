"""SSRF-safe HTTP fetchers used during discovery.

These are deliberately small and purpose-built for llms.txt generation: fetch a
page and pull out the title/description/headings/links, read robots.txt and
sitemap.xml, and check whether an llms.txt already exists.

The important part is ``_safe_url``: every outbound request is screened so the
generator can never be tricked into reaching localhost, cloud metadata
endpoints, or private/internal network ranges. This is a real safety feature —
keep it.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Comment

logger = logging.getLogger("llmstxt_generator.fetchers")

# A normal desktop-browser User-Agent. We identify as a browser because the goal
# is to read the same public HTML a person would see, not to impersonate a
# specific crawler.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

# Per-request wall-clock ceiling (seconds).
FETCH_WALLTIME_S = 12.0

# Extraction budgets (characters) so a single huge page can't blow up memory.
TEXT_BUDGET = 6000
HEADINGS_BUDGET = 1800


# ── HTTP client pool ──────────────────────────────────────────────────────────

_clients_by_ua: Dict[str, httpx.Client] = {}


def _get_client(ua: str = DEFAULT_UA) -> httpx.Client:
    cli = _clients_by_ua.get(ua)
    if cli is None:
        cli = httpx.Client(
            timeout=httpx.Timeout(FETCH_WALLTIME_S, connect=4.0, read=8.0, write=3.0, pool=3.0),
            follow_redirects=True,
            max_redirects=4,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        _clients_by_ua[ua] = cli
    return cli


# ── Safety: SSRF guard + URL hygiene ──────────────────────────────────────────


def _safe_url(url: str) -> Optional[str]:
    """Return ``None`` if the URL is safe to fetch, otherwise an error string.

    Blocks non-HTTP schemes, known metadata hosts, and any host that resolves to
    a private, loopback, link-local, multicast, reserved or unspecified address.
    """
    try:
        p = urlparse(url)
    except Exception:
        return "bad url"
    if p.scheme not in ("http", "https"):
        return "only http/https allowed"
    host = (p.hostname or "").lower().strip(".")
    if not host:
        return "no host"
    if host in ("localhost", "metadata.google.internal", "metadata.aws.internal"):
        return "blocked host"
    try:
        ip = ipaddress.ip_address(host)
        ips = [ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            ips = []
            for info in infos:
                try:
                    ips.append(ipaddress.ip_address(info[4][0]))
                except ValueError:
                    continue
        except socket.gaierror:
            return "dns failed"
    if not ips:
        return "no ip"
    for ip in ips:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return "private network blocked"
    return None


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def root_url(domain_or_url: str) -> str:
    s = normalise_url(domain_or_url)
    p = urlparse(s)
    host = p.hostname or ""
    return f"https://{host}" if host else s


def normalise_domain(domain_or_url: str) -> str:
    """Strip scheme/path/trailing slash and lowercase. ``HTTPS://X.com/foo`` -> ``x.com``."""
    s = (domain_or_url or "").strip().lower()
    if "://" in s:
        s = urlparse(s).hostname or ""
    s = s.strip("/")
    if s.startswith("www."):
        s = s[4:]
    return s


# ── Page extraction ───────────────────────────────────────────────────────────


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()


def _extract_headings(soup: BeautifulSoup) -> List[str]:
    out: List[str] = []
    used = 0
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if not text:
            continue
        line = f"{tag.name.upper()}: {text[:200]}"
        if used + len(line) > HEADINGS_BUDGET:
            break
        out.append(line)
        used += len(line)
    return out


def _visible_text(soup: BeautifulSoup) -> str:
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _parse_html(html: str, url: str, final_url: str, status: int) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = (md.get("content") or "").strip()[:400] if md else None
    mr = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots_meta = (mr.get("content") or "").strip() if mr else None
    canon = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    canonical = canon.get("href") if canon else None

    og: Dict[str, str] = {}
    for prop in ("og:title", "og:description", "og:type", "og:site_name"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            og[prop.split(":", 1)[1]] = tag["content"].strip()[:300]

    headings = _extract_headings(soup)

    host = urlparse(final_url).netloc
    internal_links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(final_url, a["href"])
        if not href.startswith(("http://", "https://")):
            continue
        if urlparse(href).netloc == host and href not in internal_links:
            internal_links.append(href)
        if len(internal_links) >= 30:
            break

    _strip_noise(soup)
    text = _visible_text(soup)
    word_count = len(text.split())
    excerpt = text[:TEXT_BUDGET]

    return {
        "url": url,
        "final_url": final_url,
        "status": status,
        "title": title[:300],
        "meta": {"description": description, "robots": robots_meta},
        "canonical": canonical,
        "og": og or None,
        "headings": headings,
        "word_count": word_count,
        "text_excerpt": excerpt,
        "internal_link_sample": internal_links[:25],
    }


def _raw_get(url: str, ua: str) -> Tuple[int, str, str, Dict[str, str]]:
    safe = _safe_url(url)
    if safe:
        return 0, "", url, {"_error": safe}
    cli = _get_client(ua)
    try:
        r = cli.get(url)
        return r.status_code, r.text, str(r.url), dict(r.headers)
    except httpx.HTTPError as e:
        return 0, "", url, {"_error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001
        return 0, "", url, {"_error": f"{type(e).__name__}: {e}"}


# ── Public fetchers ───────────────────────────────────────────────────────────


def fetch_url(url: str, ua: str = DEFAULT_UA) -> Dict[str, Any]:
    """GET a URL and extract title/meta/headings/links (or raw text for txt/xml)."""
    url = normalise_url(url)
    if not url:
        return {"error": "empty url"}
    status, html, final_url, headers = _raw_get(url, ua)
    if "_error" in headers:
        return {"url": url, "error": headers["_error"], "status": status}
    if not html:
        return {"url": url, "error": f"empty body status={status}", "status": status}

    leaf = urlparse(final_url).path.lower().rsplit("/", 1)[-1]
    ctype = (headers.get("content-type") or "").lower()
    if leaf.endswith((".txt", ".md", ".xml", ".json")) or any(
        t in ctype for t in ("text/plain", "application/json", "text/markdown", "/xml")
    ):
        body = html.strip()
        return {
            "url": url,
            "final_url": final_url,
            "status": status,
            "content_kind": "text",
            "content_type": ctype,
            "byte_size": len(html),
            "text_excerpt": body[:TEXT_BUDGET],
        }
    return _parse_html(html, url, final_url, status)


def fetch_robots_txt(domain: str, ua: str = DEFAULT_UA) -> Dict[str, Any]:
    """Fetch /robots.txt and count its User-agent rules."""
    out = fetch_url(root_url(domain) + "/robots.txt", ua)
    if out.get("error") or out.get("status") != 200:
        return out
    body = out.get("text_excerpt") or ""
    ua_rule_count = len(re.findall(r"(?im)^\s*user-agent\s*:", body))
    return {**out, "ua_rule_count": ua_rule_count}


def fetch_llms_txt(domain: str, ua: str = DEFAULT_UA) -> Dict[str, Any]:
    """Fetch /llms.txt — returns status 200 + body if the site already has one."""
    return fetch_url(root_url(domain) + "/llms.txt", ua)


def fetch_sitemap(domain_or_url: str, limit: int = 80, ua: str = DEFAULT_UA) -> Dict[str, Any]:
    """Fetch /sitemap.xml (or a specific sitemap URL) and return a URL sample.

    Follows up to the first three child sitemaps when handed a sitemap index.
    """
    if domain_or_url.lower().endswith(".xml") or "/sitemap" in domain_or_url.lower():
        url = normalise_url(domain_or_url)
    else:
        url = root_url(domain_or_url) + "/sitemap.xml"

    status, body, final_url, headers = _raw_get(url, ua)
    if "_error" in headers or not body:
        return {"url": url, "status": status, "error": headers.get("_error") or "empty body"}

    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, flags=re.I)
    is_index = "<sitemapindex" in body.lower()
    sub_sitemaps: List[str] = []
    page_urls: List[str] = []

    if is_index:
        sub_sitemaps = locs[:30]
        for sub in sub_sitemaps[:3]:
            try:
                _, sub_body, _, sub_headers = _raw_get(sub, ua)
                if sub_body and "_error" not in sub_headers:
                    page_urls.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sub_body, flags=re.I))
            except Exception:
                continue
    else:
        page_urls = locs

    seen: set = set()
    deduped: List[str] = []
    for u in page_urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
        if len(deduped) >= limit:
            break

    return {
        "url": url,
        "final_url": final_url,
        "status": status,
        "is_sitemap_index": is_index,
        "sub_sitemaps": sub_sitemaps,
        "url_sample": deduped,
        "url_sample_count": len(deduped),
    }
