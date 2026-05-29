"""Configuration + provider resolution.

The generator is model-agnostic. Any OpenAI-compatible Chat Completions endpoint
works out of the box (OpenAI, DeepSeek, Together, OpenRouter, local Ollama, ...),
and there is a native adapter for Anthropic. Everything is configurable via
environment variables or explicit arguments; arguments win over env, env wins
over the provider default.

Resolution order for each setting:
    explicit argument  ->  LLMSTXT_* env var  ->  provider default

The default provider is ``openai`` so the tool runs with just ``OPENAI_API_KEY``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .fetchers import DEFAULT_UA


@dataclass(frozen=True)
class ProviderSpec:
    base_url: Optional[str]
    model: str
    key_envs: Tuple[str, ...]
    openai_compatible: bool = True
    default_api_key: Optional[str] = None  # e.g. Ollama accepts any token


# Built-in providers. `base_url=None` means "use the SDK default" (OpenAI /
# Anthropic). Models are sensible, cheap, widely-available defaults — override
# any of them with LLMSTXT_MODEL or --model.
PROVIDERS: Dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        base_url=None,
        model="gpt-4o-mini",
        key_envs=("OPENAI_API_KEY",),
    ),
    "anthropic": ProviderSpec(
        base_url=None,
        model="claude-haiku-4-5-20251001",
        key_envs=("ANTHROPIC_API_KEY",),
        openai_compatible=False,
    ),
    "deepseek": ProviderSpec(
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        key_envs=("DEEPSEEK_API_KEY",),
    ),
    "together": ProviderSpec(
        base_url="https://api.together.xyz/v1",
        model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        key_envs=("TOGETHER_API_KEY",),
    ),
    "openrouter": ProviderSpec(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4o-mini",
        key_envs=("OPENROUTER_API_KEY",),
    ),
    "groq": ProviderSpec(
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        key_envs=("GROQ_API_KEY",),
    ),
    "ollama": ProviderSpec(
        base_url="http://localhost:11434/v1",
        model="llama3.1",
        key_envs=(),
        default_api_key="ollama",
    ),
}

DEFAULT_PROVIDER = "openai"


class ConfigError(ValueError):
    """Raised when the configuration is incomplete (e.g. no API key)."""


@dataclass
class GeneratorConfig:
    """Everything the pipeline needs to run a single generation."""

    # Model / provider
    provider: str = DEFAULT_PROVIDER
    model: str = PROVIDERS[DEFAULT_PROVIDER].model
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    openai_compatible: bool = True

    # Pipeline tuning (all have sane defaults — the cost ceiling is fixed)
    max_enrich_pages: int = 12        # extra pages fetched for real titles/metas
    max_inventory_urls: int = 60      # URLs handed to the writer to organise
    min_inventory_for_loose: int = 6  # below this, only trust URLs we actually saw
    enrich_concurrency: int = 6
    sitemap_limit: int = 120

    # Compose call
    compose_max_tokens: int = 4500
    compose_temperature: float = 0.4
    timeout: float = 90.0
    output_hard_cap: int = 60_000     # chars; refuse to ship a runaway file

    # Behaviour
    include_cold_knowledge: bool = True  # ask the model what it knows cold
    user_agent: str = DEFAULT_UA
    extra_body: Optional[Dict] = None    # escape hatch for provider-specific params

    def require_api_key(self) -> str:
        if not self.api_key:
            envs = ", ".join(PROVIDERS.get(self.provider, PROVIDERS[DEFAULT_PROVIDER]).key_envs) or "LLMSTXT_API_KEY"
            raise ConfigError(
                f"No API key for provider '{self.provider}'. "
                f"Set {envs} (or LLMSTXT_API_KEY), or pass api_key=..."
            )
        return self.api_key


def _first_env(names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return None


def resolve_config(
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    **overrides,
) -> GeneratorConfig:
    """Build a :class:`GeneratorConfig` from arguments + environment + defaults.

    Unknown provider names are treated as a generic OpenAI-compatible endpoint:
    supply ``base_url`` (or ``LLMSTXT_BASE_URL``) and an API key.
    """
    provider = (provider or os.environ.get("LLMSTXT_PROVIDER") or DEFAULT_PROVIDER).lower()
    spec = PROVIDERS.get(provider)

    if spec is not None:
        resolved_base = base_url or os.environ.get("LLMSTXT_BASE_URL") or spec.base_url
        resolved_model = model or os.environ.get("LLMSTXT_MODEL") or spec.model
        resolved_key = (
            api_key
            or os.environ.get("LLMSTXT_API_KEY")
            or _first_env(spec.key_envs)
            or spec.default_api_key
        )
        openai_compatible = spec.openai_compatible
    else:
        # Generic OpenAI-compatible endpoint identified only by its base_url.
        resolved_base = base_url or os.environ.get("LLMSTXT_BASE_URL")
        resolved_model = model or os.environ.get("LLMSTXT_MODEL")
        resolved_key = api_key or os.environ.get("LLMSTXT_API_KEY") or os.environ.get("OPENAI_API_KEY")
        openai_compatible = True
        if not resolved_base:
            raise ConfigError(
                f"Unknown provider '{provider}'. Set LLMSTXT_BASE_URL (or pass base_url=...) "
                "to point at an OpenAI-compatible endpoint, or use one of: "
                + ", ".join(sorted(PROVIDERS))
            )
        if not resolved_model:
            raise ConfigError(
                f"No model set for custom provider '{provider}'. "
                "Set LLMSTXT_MODEL (or pass model=...)."
            )

    cfg = GeneratorConfig(
        provider=provider,
        model=resolved_model,
        base_url=resolved_base,
        api_key=resolved_key,
        openai_compatible=openai_compatible,
    )
    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise TypeError(f"Unknown config override: {key!r}")
        if value is not None:
            setattr(cfg, key, value)
    return cfg
