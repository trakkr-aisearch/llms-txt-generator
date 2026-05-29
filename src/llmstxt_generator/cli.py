"""Command-line interface for llmstxt-generator.

Examples::

    llmstxt-gen stripe.com                  # print the file to stdout
    llmstxt-gen stripe.com -o llms.txt      # write it to a file
    llmstxt-gen stripe.com --verbose        # show the live discovery trace
    llmstxt-gen stripe.com --json           # full result as JSON (stats + file)

    # any OpenAI-compatible endpoint:
    llmstxt-gen stripe.com --provider deepseek
    llmstxt-gen stripe.com --provider openrouter --model anthropic/claude-3.5-haiku
    LLMSTXT_BASE_URL=http://localhost:11434/v1 llmstxt-gen stripe.com --provider ollama

stdout receives only the llms.txt (so it pipes cleanly); the trace and any
diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from . import __version__
from .builder import generate_llms_txt_stream
from .config import PROVIDERS, ConfigError, resolve_config

# ANSI colours, used only when stderr is a TTY.
_C = {
    "dim": "\033[2m", "bold": "\033[1m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "cyan": "\033[36m", "reset": "\033[0m",
}


def _color(enabled: bool):
    return _C if enabled else {k: "" for k in _C}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="llmstxt-gen",
        description="Build a high-quality llms.txt for any website. Model-agnostic; "
        "built by Trakkr (https://trakkr.ai).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Providers: " + ", ".join(sorted(PROVIDERS)) + ", or any OpenAI-compatible "
        "endpoint via --base-url. API keys come from env vars (OPENAI_API_KEY, "
        "DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, ... or LLMSTXT_API_KEY).",
    )
    p.add_argument("domain", help="The site to generate an llms.txt for, e.g. stripe.com")
    p.add_argument("-o", "--output", metavar="FILE", help="Write the file here instead of stdout.")
    p.add_argument("-v", "--verbose", action="store_true", help="Print the live discovery/compose trace to stderr.")
    p.add_argument("--json", action="store_true", help="Emit the full result (file + stats) as JSON to stdout.")

    g = p.add_argument_group("model")
    g.add_argument("--provider", help="LLM provider (default: openai, or $LLMSTXT_PROVIDER).")
    g.add_argument("--model", help="Model name (overrides the provider default / $LLMSTXT_MODEL).")
    g.add_argument("--base-url", help="OpenAI-compatible base URL (overrides $LLMSTXT_BASE_URL).")
    g.add_argument("--api-key", help="API key (overrides env; prefer env vars for secrets).")

    g2 = p.add_argument_group("tuning")
    g2.add_argument("--max-pages", type=int, metavar="N", help="Max pages to read for real titles/metas (default 12).")
    g2.add_argument("--no-cold-knowledge", action="store_true",
                    help="Skip asking the model what it knows about the brand cold.")

    p.add_argument("--version", action="version", version=f"llmstxt-generator {__version__}")
    return p


async def _run(args: argparse.Namespace) -> int:
    c = _color(sys.stderr.isatty() and not args.json)

    try:
        cfg = resolve_config(
            provider=args.provider,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            max_enrich_pages=args.max_pages,
            include_cold_knowledge=False if args.no_cold_knowledge else None,
        )
    except ConfigError as e:
        print(f"{c['red']}config error:{c['reset']} {e}", file=sys.stderr)
        return 2

    file_parts: list = []
    payload: Optional[dict] = None
    composing = False

    async for ev in generate_llms_txt_stream(args.domain, cfg):
        et = ev.get("type")
        if et == "started":
            if args.verbose:
                print(f"{c['dim']}started  provider={ev.get('provider')}  model={ev.get('model')}{c['reset']}",
                      file=sys.stderr)
        elif et == "phase":
            if args.verbose:
                print(f"\n{c['cyan']}{c['bold']}# {ev.get('label')}{c['reset']} "
                      f"{c['dim']}({ev.get('phase')}){c['reset']}", file=sys.stderr)
        elif et == "tool_started":
            if args.verbose:
                a = ev.get("args") or {}
                arg = a.get("url") or a.get("domain") or a.get("domain_or_url") or a.get("brand_or_query") or ""
                print(f"  {c['dim']}-> {ev.get('tool'):<18} {arg}{c['reset']}", file=sys.stderr)
        elif et == "tool_complete":
            if args.verbose:
                mark = f"{c['green']}ok{c['reset']}" if ev.get("ok") else f"{c['red']}!!{c['reset']}"
                print(f"  {mark} {ev.get('tool'):<18} {c['dim']}{ev.get('summary')}  "
                      f"[{ev.get('duration_ms')}ms]{c['reset']}", file=sys.stderr)
        elif et == "thinking":
            if args.verbose:
                print(f"\n  {c['yellow']}~ {ev.get('text')}{c['reset']}", file=sys.stderr)
        elif et == "composing":
            composing = True
            if args.verbose:
                print(f"\n{c['cyan']}{c['bold']}--- llms.txt (streaming) ---{c['reset']}", file=sys.stderr)
        elif et == "token":
            file_parts.append(ev.get("text", ""))
            if args.verbose:
                sys.stderr.write(ev.get("text", ""))
                sys.stderr.flush()
        elif et == "completed":
            payload = ev.get("payload")
        elif et == "error":
            print(f"\n{c['red']}error:{c['reset']} {ev.get('message')}", file=sys.stderr)
            return 1

    if not payload:
        print(f"{c['red']}error:{c['reset']} no output produced", file=sys.stderr)
        return 1

    if args.verbose:
        v = payload.get("validation", {})
        cost = payload.get("cost_usd")
        cost_str = f"  cost~${cost}" if cost is not None else ""
        print(
            f"\n{c['dim']}done  {payload['domain']}  "
            f"{v.get('link_count', 0)} links  "
            f"{payload.get('pages_read', 0)}/{payload.get('pages_discovered', 0)} pages read  "
            f"dropped={v.get('dropped_links', 0)}  "
            f"{payload['tokens']['input']}+{payload['tokens']['output']} tok"
            f"{cost_str}  {payload.get('elapsed_s')}s{c['reset']}",
            file=sys.stderr,
        )

    content = payload["llms_txt"]
    if args.json:
        out = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(out + "\n")
            print(f"wrote {args.output}", file=sys.stderr)
        else:
            print(out)
        return 0

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"wrote {args.output} ({payload['byte_size']} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def main(argv: Optional[list] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
