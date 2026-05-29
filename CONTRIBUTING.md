# Contributing

Thanks for your interest in improving `llmstxt-generator`. Contributions of all
sizes are welcome — bug reports, docs, new providers, better prompts.

## Setup

```bash
git clone https://github.com/trakkr-aisearch/llms-txt-generator
cd llmstxt-generator
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,anthropic]"
pytest        # fully offline — no network, no API keys
```

## Before you open a PR

- **Run the tests.** `pytest` must pass. The suite is offline by design (URL
  hygiene, the SSRF guard, link validation, config resolution) so it runs
  anywhere with no keys.
- **Add a test** for any behaviour change, especially around URL selection,
  link validation, or provider config.
- **Keep secrets out.** Never commit API keys, tokens, or `.env`. Keys are read
  only from environment variables at runtime. Before pushing:
  ```bash
  git diff --staged | rg -i 'sk-|api[_-]?key\s*[:=]|secret|bearer|token' || echo clean
  ```
- **Match the style.** Type hints, small focused functions, comments that
  explain *why*. No new runtime dependencies without discussion.

## Good first contributions

- Add a provider to `PROVIDERS` in `config.py` (it's a one-line `ProviderSpec`
  for any OpenAI-compatible endpoint) and a row in the README provider matrix.
- Improve URL scoring heuristics in `builder.py` (`_score_candidate`).
- Tighten the compose prompt (`COMPOSE_SYSTEM`) for a specific failure mode —
  include a before/after example in the PR.

## Reporting bugs

Open an issue with the domain, the command you ran, the provider/model, and what
you expected vs. what you got. A `--verbose` trace helps a lot.

## License

By contributing you agree your contributions are licensed under the MIT License.
