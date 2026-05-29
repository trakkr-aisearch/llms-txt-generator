"""The llms.txt generation pipeline.

Four fixed phases — no open-ended agent loop, so the cost is bounded:

    Discover  -> fetch the homepage, robots.txt, sitemap.xml and any existing
                 llms.txt, and (optionally) ask the model what it knows about the
                 brand cold.
    Enrich    -> pick the highest-signal pages and fetch their real titles/metas.
    Compose   -> one streamed model call writes the llms.txt live.
    Finalize  -> strip fences, validate every link against what we actually saw
                 (no invented URLs), de-duplicate, and score the structure.

The pipeline yields typed events so callers can render a live trace; a one-shot
convenience (:func:`generate_llms_txt`) consumes the stream and returns the
final result.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .config import GeneratorConfig, resolve_config
from .fetchers import (
    fetch_llms_txt,
    fetch_robots_txt,
    fetch_sitemap,
    fetch_url,
    normalise_domain,
    root_url,
)
from .llm import LLMClient, Usage, estimate_cost_usd

logger = logging.getLogger("llmstxt_generator.builder")

# Slugs that almost always matter for understanding a brand. Boosts selection.
_HIGH_VALUE_SLUGS = (
    "pricing", "price", "plans", "docs", "documentation", "developer", "developers",
    "api", "about", "company", "product", "products", "platform", "features",
    "feature", "solutions", "solution", "use-cases", "use-case", "customers",
    "case-studies", "case-study", "integrations", "integration", "security",
    "blog", "guides", "guide", "resources", "help", "support", "faq", "contact",
    "enterprise", "how-it-works", "demo", "changelog", "templates", "download",
)

# Things we never want as primary links in an llms.txt.
_ASSET_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".json", ".xml", ".pdf", ".zip", ".mp4", ".mp3", ".woff", ".woff2", ".ttf",
    ".rss", ".atom",
)


# ── Compose prompt ──────────────────────────────────────────────────────────

COMPOSE_SYSTEM = """You are an expert at writing llms.txt files that follow the llmstxt.org specification.

An llms.txt file lives at a site's root (e.g. example.com/llms.txt) and gives AI models a clean, structured map of what the site is and where its important content lives. The most widely understood format for models is Markdown, so llms.txt is Markdown.

THE SPEC — produce sections in exactly this order:
1. An H1 with the site / brand name. `# Brand`  (the only strictly required line)
2. A blockquote summary: `> ...` — one or two sentences of plain factual description (what the company is and does, who it serves). NOT a tagline, NOT marketing fluff. State concrete facts a model could quote.
3. Optionally, one short paragraph of extra context (positioning, scale, notable facts). No headings here.
4. One or more `## Section` headings, each followed by a Markdown list of links in the form:
   `- [Page title](https://full-url): a specific one-line description of what the page covers.`
5. A final `## Optional` section for secondary pages (legal, careers, changelog, status, etc.) that a model can skip under tight context.

QUALITY BAR — this output is shown to a human who will judge it instantly. Make it look authoritative and clean:
- Blockquote must be crisp and confident, like a knowledgeable analyst wrote it. Lead with what the company IS.
- Group links into 4-8 well-named sections that reflect the site's real structure (e.g. Product, Pricing, Documentation, Company, Resources). Section names are specific noun phrases, not generic ("Links", "Pages").
- Descriptions are specific and useful: say what the page actually contains and what a model would learn, not "Learn more about X." Include concrete nouns. ~6-18 words each.
- Order sections by importance: what the site is/does first, supporting material later, the `## Optional` block last.
- Aim for roughly 18-40 links total. Quality and coverage over padding.
- The `## Optional` section is for a SMALL curated set (3-6 links) of genuinely useful secondary pages (contact, status, changelog, one or two key legal pages). Do NOT exhaustively dump every legal, policy, or terms page — pick the most useful and drop the rest.

HARD RULES — non-negotiable:
- Use ONLY URLs that appear in the SITE DATA provided. Never invent, guess, or "complete" a URL. If you are unsure a URL exists, leave it out.
- Where a page's real title/description is given, ground your description in it. For URLs given without a title, write a short factual description inferred from the URL path and overall site context — keep it generic rather than inventing specifics (numbers, dates, claims).
- Do NOT state volatile figures — company valuation, funding raised, revenue, employee or customer counts, founding year — unless they appear verbatim in the SITE DATA. A stale number is worse than no number. The "cold knowledge" block in particular may be out of date; treat it as a hint about what the company does, not as a source of facts to quote.
- Keep URL style consistent: do not add locale prefixes (like /gb/ or /us/) that aren't already in the provided URLs.
- Plain ASCII punctuation. No emojis. No first-person ("we", "our"). Third-person, factual.
- Output ONLY the raw llms.txt Markdown. No code fences, no preamble, no closing commentary. Start directly with `# `."""

_COLD_SYSTEM = (
    "You are answering as if you were ChatGPT/Claude cold, no search. "
    "Reply with what you actually know about the entity asked. If you "
    "do not know it, say so plainly. Be specific. 4-8 short bullets."
)


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass
class LlmsTxtResult:
    """The output of a successful generation."""

    domain: str
    content: str                       # the llms.txt file itself
    requested_domain: str = ""
    structure: Dict[str, Any] = field(default_factory=dict)
    validation: Dict[str, Any] = field(default_factory=dict)
    pages_discovered: int = 0
    pages_read: int = 0
    limited_discovery: bool = False
    existing_llms_txt_found: bool = False
    provider: str = ""
    model: str = ""
    tokens: Dict[str, int] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    elapsed_s: float = 0.0

    def __str__(self) -> str:  # so `print(result)` shows the file
        return self.content


class GenerationError(RuntimeError):
    pass


# ── URL helpers ───────────────────────────────────────────────────────────────


def _host(url_or_domain: str) -> str:
    """Lowercased host (keeps subdomains; does NOT strip www)."""
    s = (url_or_domain or "").strip().lower()
    if "://" in s:
        return urlparse(s).hostname or ""
    return urlparse("https://" + s).hostname or s


def _same_site(url: str, apex: str) -> bool:
    """True if ``url``'s host is the apex domain or a subdomain of it."""
    h = _host(url)
    a = apex.lower()
    if a.startswith("www."):
        a = a[4:]
    if h.startswith("www."):
        h = h[4:]
    return bool(h) and (h == a or h.endswith("." + a))


def _sitemaps_from_robots(body: str) -> List[str]:
    return [m.strip() for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)\s*$", body or "")]


# Country/language path prefixes to collapse so a file doesn't mix
# example.com/pricing with example.com/gb/pricing. Conservative, common set.
_LOCALE_SEGMENTS = {
    "us", "uk", "gb", "ca", "au", "nz", "ie", "in", "sg", "za", "fr", "de", "es",
    "it", "nl", "be", "ch", "at", "se", "no", "dk", "fi", "pl", "pt", "br", "mx",
    "ar", "cl", "co", "jp", "kr", "cn", "tw", "hk", "ru", "tr", "ae", "sa", "id",
    "th", "vn", "ph", "my", "en", "ja", "ko", "zh", "fr-fr", "en-us", "en-gb",
    "de-de", "es-es", "es-mx", "pt-br", "zh-cn", "zh-tw", "ja-jp", "fr-ca",
}
_LANG_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]{2}$")


def _strip_locale(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    if segs and (segs[0] in _LOCALE_SEGMENTS or _LANG_REGION_RE.match(segs[0])):
        segs = segs[1:]
    return "/" + "/".join(segs)


def _norm_url(url: str) -> str:
    """Normalise for dedupe + whitelist + display."""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url.strip()
    if not p.scheme:
        return url.strip()
    host = (p.netloc or "").lower()
    path = _strip_locale(p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    out = f"{p.scheme}://{host}{path}"
    if p.query:
        out += f"?{p.query}"
    return out


def _path_depth(url: str) -> int:
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return 9
    return 0 if not path else len([seg for seg in path.split("/") if seg])


def _is_asset(url: str) -> bool:
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return path.endswith(_ASSET_EXT)


def _slug_score(url: str) -> int:
    low = url.lower()
    return sum(1 for s in _HIGH_VALUE_SLUGS if f"/{s}" in low or low.rstrip("/").endswith("/" + s))


def _score_candidate(url: str) -> float:
    """Higher = more worth fetching/including. Favours shallow, high-value pages."""
    depth = _path_depth(url)
    score = 10.0 - depth * 2.0
    score += _slug_score(url) * 4.0
    low = url.lower()
    if any(t in low for t in ("/tag/", "/category/", "/page/", "/author/", "?page=", "/feed")):
        score -= 6.0
    if any(t in low for t in (
        "/legal", "/policy", "/policies", "/terms", "/privacy", "/dpa", "/gdpr",
        "/cookie", "/compliance", "/trust/", "/ssa",
    )):
        score -= 5.0
    if "?" in low:
        score -= 2.0
    return score


def _select_candidates(domain: str, homepage_links: List[str], sitemap_urls: List[str]) -> List[str]:
    """Same-site, non-asset, deduped, importance-sorted URL inventory."""
    seen: set = set()
    scored: List[Tuple[float, str]] = []
    root = _norm_url(root_url(domain) + "/")
    for raw in list(homepage_links or []) + list(sitemap_urls or []):
        if not raw:
            continue
        nu = _norm_url(raw)
        if nu in seen or nu == root:
            continue
        if not _same_site(nu, domain):
            continue
        if not nu.startswith(("http://", "https://")):
            continue
        if _is_asset(nu):
            continue
        seen.add(nu)
        scored.append((_score_candidate(nu), nu))
    scored.sort(key=lambda x: (-x[0], _path_depth(x[1]), x[1]))
    return [u for _, u in scored]


def _page_facts(result: Dict[str, Any], apex: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Pull the compose-relevant facts out of a fetch_url result."""
    if not isinstance(result, dict) or result.get("error"):
        return None
    if result.get("status") not in (200, 201):
        return None
    title = (result.get("title") or "").strip()
    meta = result.get("meta") or {}
    desc = (meta.get("description") or "").strip()
    og = result.get("og") or {}
    if not desc and isinstance(og, dict):
        desc = (og.get("description") or "").strip()
    headings = result.get("headings") or []
    h1 = ""
    for h in headings:
        if isinstance(h, str) and h.upper().startswith("H1:"):
            h1 = h[3:].strip()
            break
    if not (title or desc or h1):
        return None
    url = result.get("final_url") or result.get("url") or ""
    canon = (result.get("canonical") or "").strip()
    if canon.startswith(("http://", "https://")) and (apex is None or _same_site(canon, apex)):
        url = canon
    return {
        "url": _norm_url(url),
        "title": title[:140],
        "description": desc[:240],
        "h1": h1[:140],
    }


def _summary_for(tool: str, result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return str(result)[:120]
    if result.get("error"):
        return f"error: {str(result['error'])[:100]}"
    if tool == "fetch_url":
        return f"{result.get('status')} - {result.get('word_count', 0)} words - {(result.get('title') or '')[:50]}"
    if tool == "fetch_robots_txt":
        return f"{result.get('status')} - {result.get('ua_rule_count', 0)} UA rules"
    if tool == "fetch_sitemap":
        return f"{result.get('status')} - {result.get('url_sample_count', 0)} URLs - index={result.get('is_sitemap_index')}"
    if tool == "fetch_llms_txt":
        return "200 - already exists" if result.get("status") == 200 else f"{result.get('status')} (none yet)"
    if tool == "ask_llm_knowledge":
        return f"cold prior: {(result.get('cold_knowledge') or '')[:80]}"
    return str(result.get("status") or "ok")[:80]


def _build_site_data_block(
    *,
    domain: str,
    homepage: Dict[str, Any],
    cold_knowledge: str,
    existing_llms: Optional[str],
    enriched: List[Dict[str, str]],
    extra_urls: List[str],
) -> str:
    """The grounding context handed to the writer. URLs here are the whitelist."""
    root = root_url(domain)
    parts: List[str] = [f"SITE: {domain}", f"ROOT URL: {root}"]

    hp_facts = _page_facts(homepage, domain) or {}
    hp_title = hp_facts.get("title") or (homepage.get("title") or "")
    hp_desc = hp_facts.get("description") or ""
    hp_h1 = hp_facts.get("h1") or ""
    og = homepage.get("og") or {}
    parts.append("\n## Homepage")
    parts.append(f"URL: {root}/")
    if hp_title:
        parts.append(f"Title: {hp_title}")
    if hp_h1:
        parts.append(f"H1: {hp_h1}")
    if hp_desc:
        parts.append(f"Meta description: {hp_desc}")
    if isinstance(og, dict) and og.get("site_name"):
        parts.append(f"og:site_name: {og.get('site_name')}")
    headings = [h for h in (homepage.get("headings") or []) if isinstance(h, str)][:12]
    if headings:
        parts.append("Homepage headings:\n  " + "\n  ".join(headings))

    if cold_knowledge:
        parts.append(
            "\n## What AI models already say about this brand (cold, no browsing)\n"
            "Use this only to sharpen the factual blockquote — do NOT cite it as page content.\n"
            + cold_knowledge.strip()[:1200]
        )

    if existing_llms:
        parts.append(
            "\n## The site's CURRENT llms.txt (improve on this; keep what's accurate)\n"
            + existing_llms.strip()[:2500]
        )

    if enriched:
        parts.append("\n## Key pages (real titles + descriptions — ground your link descriptions in these)")
        for p in enriched:
            line = f"- {p['url']}"
            if p.get("title"):
                line += f"\n  Title: {p['title']}"
            if p.get("description"):
                line += f"\n  Description: {p['description']}"
            elif p.get("h1"):
                line += f"\n  H1: {p['h1']}"
            parts.append(line)

    if extra_urls:
        parts.append(
            "\n## Other discovered URLs on this site (real URLs — include the relevant ones, "
            "write short factual descriptions from the path)\n"
            + "\n".join(f"- {u}" for u in extra_urls)
        )

    return "\n".join(parts)


# ── URL whitelist validation (anti-hallucination) ────────────────────────────

_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^\s)]+)\)")


def _validate_and_clean(
    text: str, allowed: set, apex: str, *, strict: bool
) -> Tuple[str, Dict[str, Any]]:
    """Drop link lines we can't stand behind, and de-duplicate by URL.

    A list item that links to a URL is kept when:
      - the URL is one we actually discovered (in ``allowed``), OR
      - ``strict`` is False AND the URL is on the site's own host.
    When ``strict`` is True (sparse discovery, e.g. a bot-walled site) we only
    trust URLs we actually saw, so the model can't fabricate a whole site map.
    Every URL may appear at most once.
    """
    allowed_norm = {_norm_url(u) for u in allowed}
    kept_lines: List[str] = []
    seen_urls: set = set()
    dropped_invented = 0
    dropped_dupe = 0
    dropped_offhost = 0
    link_count = 0

    for line in text.splitlines():
        stripped = line.strip()
        m = _LINK_RE.search(stripped)
        is_list_link = stripped.startswith(("- ", "* ", "+ ")) and m is not None
        if not is_list_link:
            kept_lines.append(line)
            continue
        nu = _norm_url(m.group(2))
        on_host = _same_site(nu, apex)
        known = nu in allowed_norm
        allow = known or (on_host and not strict)
        if not allow:
            dropped_invented += 1
            if not on_host:
                dropped_offhost += 1
            continue
        if nu in seen_urls:
            dropped_dupe += 1
            continue
        seen_urls.add(nu)
        kept_lines.append(line)
        link_count += 1

    cleaned = "\n".join(kept_lines)
    cleaned = _drop_empty_sections(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"
    return cleaned, {
        "dropped_links": dropped_invented + dropped_dupe,
        "dropped_invented_links": dropped_invented,
        "dropped_offhost_links": dropped_offhost,
        "dropped_duplicate_links": dropped_dupe,
        "link_count": link_count,
        "strict_mode": strict,
    }


def _drop_empty_sections(text: str) -> str:
    """Remove ``## Heading`` blocks that lost all their links to validation."""
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^##\s+\S", line):
            j = i + 1
            has_link = False
            while j < len(lines) and not re.match(r"^##\s+\S", lines[j]):
                if _LINK_RE.search(lines[j]) and lines[j].strip().startswith(("- ", "* ", "+ ")):
                    has_link = True
                j += 1
            if not has_link:
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        if t.endswith("```"):
            t = t[:-3]
    idx = t.find("# ")
    if idx > 0 and "\n" in t[:idx]:
        head = t[:idx]
        if not head.strip().startswith("#"):
            t = t[idx:]
    return t.strip()


def _structure_stats(text: str) -> Dict[str, Any]:
    sections = re.findall(r"^##\s+(.+)$", text, flags=re.M)
    has_h1 = bool(re.search(r"^#\s+\S", text, flags=re.M))
    has_blockquote = bool(re.search(r"^>\s+\S", text, flags=re.M))
    has_optional = any(s.strip().lower() == "optional" for s in sections)
    return {
        "has_h1": has_h1,
        "has_blockquote": has_blockquote,
        "section_count": len(sections),
        "sections": [s.strip() for s in sections],
        "has_optional_section": has_optional,
    }


def _ask_llm_knowledge(client: LLMClient, brand: str) -> Dict[str, Any]:
    """Sync cold-knowledge prior. Returns the same shape regardless of provider."""
    user_p = (
        f"What do you know about {brand.strip()}? Cover: what they do, who "
        "they serve, notable products, any well-known facts, any recent reputational "
        "signals you recall."
    )
    try:
        c = client.complete(_COLD_SYSTEM, user_p, max_tokens=600, temperature=0.2)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return {
        "query": brand,
        "cold_knowledge": c.text,
        "input_tokens": c.usage.input_tokens,
        "output_tokens": c.usage.output_tokens,
    }


# ── The pipeline ──────────────────────────────────────────────────────────────


async def generate_llms_txt_stream(
    domain: str,
    config: Optional[GeneratorConfig] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run the 4-phase pipeline, yielding trace events.

    Event types: ``started``, ``phase``, ``tool_started``, ``tool_complete``,
    ``thinking``, ``composing``, ``token``, ``completed``, ``error``.
    The final ``completed`` event carries ``payload`` (a serialisable dict).
    """
    cfg = config or resolve_config()

    domain = normalise_domain(domain)
    if not domain:
        yield {"type": "error", "message": "empty domain"}
        return

    try:
        client = LLMClient(cfg)
    except Exception as e:  # noqa: BLE001  (ConfigError / LLMError)
        yield {"type": "error", "message": str(e)}
        return

    started = time.time()
    yield {"type": "started", "domain": domain, "provider": cfg.provider, "model": cfg.model}

    tool_seq = 0

    def _next_id() -> str:
        nonlocal tool_seq
        tool_seq += 1
        return f"t{tool_seq}"

    async def _run_tool(tool: str, fn, args: Dict[str, Any]):
        tid = _next_id()
        ev_started = {"type": "tool_started", "id": tid, "tool": tool, "args": args}
        t0 = time.time()
        try:
            result = await asyncio.to_thread(fn)
        except Exception as e:  # noqa: BLE001
            result = {"error": f"{type(e).__name__}: {e}"}
        ev_complete = {
            "type": "tool_complete",
            "id": tid,
            "tool": tool,
            "ok": isinstance(result, dict) and not result.get("error"),
            "duration_ms": int((time.time() - t0) * 1000),
            "summary": _summary_for(tool, result if isinstance(result, dict) else {}),
        }
        return ev_started, ev_complete, result

    ua = cfg.user_agent

    # ── Phase 1: Discover ─────────────────────────────────────────────────────
    yield {"type": "phase", "phase": "discover", "label": "Mapping the site"}

    root = root_url(domain)
    discover_specs = [
        ("fetch_url", lambda: fetch_url(root + "/", ua), {"url": root + "/"}),
        ("fetch_sitemap", lambda: fetch_sitemap(domain, cfg.sitemap_limit, ua), {"domain": domain}),
        ("fetch_robots_txt", lambda: fetch_robots_txt(domain, ua), {"domain": domain}),
        ("fetch_llms_txt", lambda: fetch_llms_txt(domain, ua), {"domain": domain}),
    ]
    if cfg.include_cold_knowledge:
        discover_specs.append(
            ("ask_llm_knowledge", lambda: _ask_llm_knowledge(client, domain), {"brand_or_query": domain})
        )

    tasks = [asyncio.create_task(_run_tool(t, fn, a)) for t, fn, a in discover_specs]
    results: Dict[str, Dict[str, Any]] = {}
    for fut in asyncio.as_completed(tasks):
        ev_started, ev_complete, result = await fut
        yield ev_started
        yield ev_complete
        results[ev_started["tool"]] = result

    homepage = results.get("fetch_url") or {}
    if homepage.get("error"):
        yield {"type": "error", "message": f"Could not fetch {domain}: {homepage.get('error')}"}
        return

    sitemap = results.get("fetch_sitemap") or {}
    robots = results.get("fetch_robots_txt") or {}
    existing = results.get("fetch_llms_txt") or {}
    cold = results.get("ask_llm_knowledge") or {}
    cold_tokens_in = cold.get("input_tokens", 0) if isinstance(cold, dict) else 0
    cold_tokens_out = cold.get("output_tokens", 0) if isinstance(cold, dict) else 0

    # Effective site identity = where the homepage actually resolved (handles
    # apex->www and rebrand redirects so same-site matching uses the real host).
    final_home = homepage.get("final_url") or (root + "/")
    eff_host = _host(final_home) or normalise_domain(domain)
    site = eff_host[4:] if eff_host.startswith("www.") else eff_host
    eff_root = root_url(site)

    # Fallback: many sites declare a `Sitemap:` directive in robots.txt instead
    # of serving /sitemap.xml. Follow the first same-site one if the default 404'd.
    if (sitemap.get("url_sample_count") or 0) == 0:
        declared = _sitemaps_from_robots(robots.get("text_excerpt") or "")
        declared = [u for u in declared if _same_site(u, site)]
        if declared:
            ev_started, ev_complete, sm2 = await _run_tool(
                "fetch_sitemap",
                lambda u=declared[0]: fetch_sitemap(u, cfg.sitemap_limit, ua),
                {"domain_or_url": declared[0]},
            )
            yield ev_started
            yield ev_complete
            if (sm2.get("url_sample_count") or 0) > (sitemap.get("url_sample_count") or 0):
                sitemap = sm2

    homepage_links = homepage.get("internal_link_sample") or []
    sitemap_urls = sitemap.get("url_sample") or []
    candidates = _select_candidates(site, homepage_links, sitemap_urls)
    strict = len(candidates) < cfg.min_inventory_for_loose

    existing_llms_body = None
    if isinstance(existing, dict) and existing.get("status") == 200:
        existing_llms_body = existing.get("text_excerpt") or None

    yield {
        "type": "thinking",
        "text": (
            f"Discovered {len(candidates)} candidate pages "
            f"({len(homepage_links)} from the homepage, {len(sitemap_urls)} from the sitemap). "
            + (
                "An llms.txt already exists - it will be improved on. "
                if existing_llms_body
                else "No llms.txt found yet. "
            )
            + f"Reading the {min(cfg.max_enrich_pages, len(candidates))} highest-signal pages next."
        ),
    }

    # ── Phase 2: Enrich ───────────────────────────────────────────────────────
    yield {"type": "phase", "phase": "enrich", "label": "Reading the key pages"}

    to_enrich = candidates[: cfg.max_enrich_pages]
    enriched: List[Dict[str, str]] = []
    sem = asyncio.Semaphore(cfg.enrich_concurrency)

    async def _enrich_one(url: str):
        async with sem:
            return await _run_tool("fetch_url", lambda: fetch_url(url, ua), {"url": url})

    enrich_tasks = [asyncio.create_task(_enrich_one(u)) for u in to_enrich]
    for fut in asyncio.as_completed(enrich_tasks):
        ev_started, ev_complete, result = await fut
        yield ev_started
        yield ev_complete
        facts = _page_facts(result, site)
        if facts:
            enriched.append(facts)

    enriched_urls = {_norm_url(p["url"]) for p in enriched}
    extra_urls = [u for u in candidates if _norm_url(u) not in enriched_urls][
        : max(0, cfg.max_inventory_urls - len(enriched))
    ]

    allowed: set = {_norm_url(eff_root + "/")}
    allowed.update(_norm_url(u) for u in candidates)
    allowed.update(p["url"] for p in enriched)

    site_data = _build_site_data_block(
        domain=site,
        homepage=homepage,
        cold_knowledge=cold.get("cold_knowledge", "") if isinstance(cold, dict) else "",
        existing_llms=existing_llms_body,
        enriched=enriched,
        extra_urls=extra_urls,
    )

    # ── Phase 3: Compose (streamed) ───────────────────────────────────────────
    yield {"type": "phase", "phase": "compose", "label": "Writing llms.txt"}
    yield {"type": "composing"}

    user_msg = (
        f"Write the llms.txt for {site}. Use only the URLs in the SITE DATA below.\n\n"
        f"=== SITE DATA ===\n{site_data}\n=== END SITE DATA ===\n\n"
        "Write the complete llms.txt now, starting with the H1."
    )

    raw_chunks: List[str] = []
    try:
        async for piece in client.stream(
            COMPOSE_SYSTEM,
            user_msg,
            max_tokens=cfg.compose_max_tokens,
            temperature=cfg.compose_temperature,
        ):
            raw_chunks.append(piece)
            yield {"type": "token", "text": piece}
            await asyncio.sleep(0)
            if sum(len(c) for c in raw_chunks) > cfg.output_hard_cap:
                break
    except Exception as e:  # noqa: BLE001
        logger.exception("llms.txt compose failed for %s", domain)
        yield {"type": "error", "message": f"compose failed: {type(e).__name__}: {e}"}
        return

    raw = "".join(raw_chunks)
    if not raw.strip():
        yield {"type": "error", "message": "model returned an empty file"}
        return

    compose_usage: Usage = client.last_usage

    # ── Phase 4: Finalize ─────────────────────────────────────────────────────
    cleaned = _strip_fences(raw)
    cleaned, validation = _validate_and_clean(cleaned, allowed, site, strict=strict)
    stats = _structure_stats(cleaned)

    total_in = compose_usage.input_tokens + cold_tokens_in
    total_out = compose_usage.output_tokens + cold_tokens_out
    payload = {
        "domain": site,
        "requested_domain": domain,
        "llms_txt": cleaned,
        "byte_size": len(cleaned.encode("utf-8")),
        "structure": stats,
        "validation": validation,
        "pages_discovered": len(candidates),
        "pages_read": len(enriched),
        "limited_discovery": strict,
        "existing_llms_txt_found": bool(existing_llms_body),
        "provider": cfg.provider,
        "model": cfg.model,
        "tokens": {"input": total_in, "output": total_out},
        "cost_usd": estimate_cost_usd(cfg.model, total_in, total_out),
        "elapsed_s": round(time.time() - started, 2),
    }
    yield {"type": "completed", "payload": payload}


def _result_from_payload(payload: Dict[str, Any]) -> LlmsTxtResult:
    return LlmsTxtResult(
        domain=payload["domain"],
        content=payload["llms_txt"],
        requested_domain=payload.get("requested_domain", ""),
        structure=payload.get("structure", {}),
        validation=payload.get("validation", {}),
        pages_discovered=payload.get("pages_discovered", 0),
        pages_read=payload.get("pages_read", 0),
        limited_discovery=payload.get("limited_discovery", False),
        existing_llms_txt_found=payload.get("existing_llms_txt_found", False),
        provider=payload.get("provider", ""),
        model=payload.get("model", ""),
        tokens=payload.get("tokens", {}),
        cost_usd=payload.get("cost_usd"),
        elapsed_s=payload.get("elapsed_s", 0.0),
    )


async def generate_llms_txt_async(
    domain: str,
    config: Optional[GeneratorConfig] = None,
    **overrides,
) -> LlmsTxtResult:
    """Async one-shot: run the pipeline and return the final result.

    Extra keyword arguments (``provider=``, ``model=``, ``api_key=``, ...) are
    forwarded to :func:`resolve_config` when ``config`` is not supplied.
    """
    cfg = config or resolve_config(**overrides)
    final: Optional[Dict[str, Any]] = None
    async for ev in generate_llms_txt_stream(domain, cfg):
        if ev.get("type") == "completed":
            final = ev.get("payload")
        elif ev.get("type") == "error":
            raise GenerationError(ev.get("message") or "generation failed")
    if not final:
        raise GenerationError("no completion event")
    return _result_from_payload(final)


def generate_llms_txt(
    domain: str,
    config: Optional[GeneratorConfig] = None,
    **overrides,
) -> LlmsTxtResult:
    """Synchronous one-shot. The simplest entry point.

    >>> from llmstxt_generator import generate_llms_txt
    >>> result = generate_llms_txt("stripe.com")   # needs OPENAI_API_KEY
    >>> print(result.content)
    """
    return asyncio.run(generate_llms_txt_async(domain, config, **overrides))
