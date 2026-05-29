# llmstxt-generator

**Build a high-quality [`llms.txt`](https://llmstxt.org) for any website — from one command.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Built by Trakkr](https://img.shields.io/badge/built%20by-Trakkr-0a0a0a.svg)](https://trakkr.ai)

```bash
llmstxt-gen stripe.com
```

It crawls a site the same way an AI agent would — homepage, sitemap, robots.txt,
the highest-signal pages — and writes a clean, spec-compliant `llms.txt` that
gives models a faithful map of what the site is and where its important content
lives. Every link in the output is one the generator actually saw: **no invented
URLs.**

Model-agnostic by design. Runs against OpenAI, Anthropic, DeepSeek, Together,
OpenRouter, Groq, or a local Ollama with a single flag.

> Built by **[Trakkr](https://trakkr.ai)** — the AI visibility platform. This is
> the open-source engine behind Trakkr's free llms.txt tool.

---

## What is `llms.txt`?

`llms.txt` is a simple Markdown file at a site's root (`example.com/llms.txt`)
that tells AI models and agents what a site is about and which pages matter,
without making them wade through navigation, scripts, and boilerplate. Think of
it as `robots.txt` for *meaning* instead of *access* — a curated, machine-readable
index of your most important content. The format is defined at
[llmstxt.org](https://llmstxt.org).

It's moving from convention to standard. In **May 2026, Google added an
`llms.txt` check to Lighthouse's new Agentic Browsing audit**, putting it
alongside the performance and accessibility signals teams already track. A good
`llms.txt` is fast becoming table stakes for being well-represented in AI search
and assistants.

## Quickstart

```bash
pip install llmstxt-generator      # or: pipx install llmstxt-generator
# or install the latest straight from source:
#   pip install "git+https://github.com/trakkr-aisearch/llms-txt-generator"
export OPENAI_API_KEY=sk-...        # the only thing the default needs

llmstxt-gen stripe.com             # print to stdout
llmstxt-gen stripe.com -o llms.txt # write to a file
llmstxt-gen stripe.com --verbose   # watch the live discovery trace
```

As a library:

```python
from llmstxt_generator import generate_llms_txt

result = generate_llms_txt("stripe.com")   # needs OPENAI_API_KEY
print(result.content)

print(result.pages_read, "pages read")
print(result.validation["link_count"], "links")
print(result.validation["dropped_invented_links"], "hallucinated URLs dropped")
```

## Example output

<!-- EXAMPLE_OUTPUT_START -->
Real output from `llmstxt-gen stripe.com` (default model, ~$0.001, ~20s, 23
links, 0 hallucinated URLs dropped). Trimmed for length — the full files for
Stripe, Vercel, and Anthropic are in [`examples/`](examples/).

```markdown
# Stripe

> Stripe is a financial services platform that provides businesses with tools to
> accept payments, manage financial operations, and implement custom revenue
> models. It serves a diverse range of clients, from startups to large
> enterprises, across various industries.

## Payments Solutions

- [Stripe Payments](https://stripe.com/payments): Accept payments online and in person globally with a payments solution built for any business.
- [Payment methods](https://stripe.com/payments/payment-methods): Explore popular local payment methods to improve conversion rates for businesses.
- [Stripe Payments documentation](https://docs.stripe.com/payments.md): A guide to integrating Stripe's payments APIs.

## Connect Solutions

- [Stripe Connect](https://stripe.com/connect): Embed payments into products with seamless onboarding and global payouts.
- [Marketplace payments](https://stripe.com/connect/marketplaces): Tools for onboarding and paying out freelancers and sellers.

## Enterprise Solutions

- [Enterprise Payment Solutions for Large Businesses](https://stripe.com/enterprise): Tailored financial solutions for large enterprises.
- [Pricing & Fees](https://stripe.com/pricing): Details on Stripe's processing fees and pricing models for businesses.

## Optional

- [Stripe Newsroom](https://stripe.com/newsroom): Latest news and updates about Stripe's partnerships and innovations.
- [Legal](https://stripe.com/legal): Access Stripe's legal documents and policies.
```
<!-- EXAMPLE_OUTPUT_END -->

## Use any model

The default is OpenAI's `gpt-4o-mini` (cheap, fast, widely available). Switch
providers with a flag or an env var — any **OpenAI-compatible** Chat Completions
endpoint works, plus a native Anthropic adapter.

```bash
llmstxt-gen stripe.com --provider deepseek
llmstxt-gen stripe.com --provider anthropic --model claude-haiku-4-5-20251001
llmstxt-gen stripe.com --provider openrouter --model openai/gpt-4o-mini
LLMSTXT_PROVIDER=ollama llmstxt-gen stripe.com   # local, no key
```

### Provider matrix

| Provider | `--provider` | API key env | Default model | Notes |
|---|---|---|---|---|
| OpenAI | `openai` *(default)* | `OPENAI_API_KEY` | `gpt-4o-mini` | Works out of the box |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` | `pip install 'llmstxt-generator[anthropic]'` |
| DeepSeek | `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` | OpenAI-compatible |
| Together | `together` | `TOGETHER_API_KEY` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | OpenAI-compatible |
| OpenRouter | `openrouter` | `OPENROUTER_API_KEY` | `openai/gpt-4o-mini` | Any model on OpenRouter |
| Groq | `groq` | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | OpenAI-compatible |
| Ollama | `ollama` | _(none)_ | `llama3.1` | Local `http://localhost:11434/v1` |
| Any other | _custom_ | `LLMSTXT_API_KEY` | _set `--model`_ | Point `--base-url` at any OpenAI-compatible API |

Override anything via env: `LLMSTXT_PROVIDER`, `LLMSTXT_MODEL`,
`LLMSTXT_BASE_URL`, `LLMSTXT_API_KEY`. Arguments beat env; env beats defaults.

```bash
# A custom OpenAI-compatible gateway:
LLMSTXT_BASE_URL=https://my-gateway.internal/v1 \
LLMSTXT_API_KEY=... \
llmstxt-gen stripe.com --provider custom --model my-model
```

## How it works

A fixed **four-phase pipeline** — no open-ended agent loop, so the cost and
runtime are bounded and predictable (roughly a cent or less per site on the
default model).

```
1. Discover  ──  fetch the homepage, robots.txt, sitemap.xml, and any existing
                 llms.txt; optionally ask the model what it knows about the brand
                 cold (to sharpen the summary, never to invent page content).

2. Enrich    ──  score every discovered URL (shallow + high-value slugs win),
                 then fetch the top pages for their real titles and descriptions.

3. Compose   ──  one streamed model call writes the llms.txt live, grounded only
                 in the pages we actually read.

4. Finalize  ──  strip code fences, validate every link against what we saw,
                 de-duplicate, drop emptied sections, and score the structure.
```

**No hallucinated URLs.** Phase 4 checks every link against the set of URLs the
crawler actually discovered. When discovery is rich, on-site URLs the model
assembled from real context are allowed; when discovery is sparse (a bot-walled
or JS-only site), it switches to **strict mode** and keeps *only* URLs it
literally saw — so the model can't fabricate a site map from memory. Duplicate
links (the "eleven titles all pointing at the homepage" failure) are collapsed.

**Same-site only, redirect-aware.** Links are constrained to the apex domain and
its subdomains. The effective host is taken from where the homepage *actually
resolved*, so apex→www and rebrand redirects are handled correctly.

**SSRF-safe.** Every outbound fetch is screened by `_safe_url`: non-HTTP schemes,
`localhost`, cloud metadata endpoints, and private / loopback / link-local /
reserved IP ranges are all refused. Safe to point at user-supplied domains.

## CLI reference

```
llmstxt-gen DOMAIN [options]

  -o, --output FILE       Write the file here instead of stdout.
  -v, --verbose           Print the live discovery/compose trace to stderr.
      --json              Emit the full result (file + stats) as JSON.

  --provider NAME         openai | anthropic | deepseek | together |
                          openrouter | groq | ollama | <custom>
  --model NAME            Override the provider's default model.
  --base-url URL          OpenAI-compatible base URL (for custom endpoints).
  --api-key KEY           API key (prefer env vars for secrets).

  --max-pages N           Max pages to read for real titles/metas (default 12).
  --no-cold-knowledge     Skip the cold-knowledge prior.
  --version
```

stdout receives only the `llms.txt`, so it pipes cleanly; the trace and
diagnostics go to stderr.

## Library API

```python
from llmstxt_generator import (
    generate_llms_txt,         # sync, returns LlmsTxtResult
    generate_llms_txt_async,   # async, returns LlmsTxtResult
    generate_llms_txt_stream,  # async generator of trace events
    resolve_config,            # build a GeneratorConfig from env/args
    GeneratorConfig,
)

# Override provider/model/tuning inline:
result = generate_llms_txt("stripe.com", provider="deepseek", max_enrich_pages=20)

# Or stream the trace yourself:
import asyncio
async def main():
    async for event in generate_llms_txt_stream("stripe.com"):
        print(event["type"])
asyncio.run(main())
```

`LlmsTxtResult` carries `content`, `structure`, `validation`, `pages_read`,
`pages_discovered`, `tokens`, `cost_usd`, `elapsed_s`, and more.

## Development

```bash
git clone https://github.com/trakkr-aisearch/llms-txt-generator
cd llmstxt-generator
pip install -e ".[dev]"
pytest          # the test suite is fully offline — no network, no API key
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT © [Trakkr](https://trakkr.ai). See [LICENSE](LICENSE).

---

Made by **[Trakkr](https://trakkr.ai)** — track and improve how your brand shows
up in ChatGPT, Perplexity, Gemini, Google AI Overviews, and Claude. If this tool
is useful, [Trakkr](https://trakkr.ai) is the platform behind it.
