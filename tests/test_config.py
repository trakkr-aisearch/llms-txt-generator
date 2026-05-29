"""Provider/config resolution + cost estimation — all offline."""
import pytest

from llmstxt_generator.config import ConfigError, resolve_config
from llmstxt_generator.llm import estimate_cost_usd


def test_default_is_openai(monkeypatch):
    for var in ("LLMSTXT_PROVIDER", "LLMSTXT_MODEL", "LLMSTXT_BASE_URL", "LLMSTXT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = resolve_config()
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.api_key == "sk-test"
    assert cfg.openai_compatible


def test_deepseek_defaults(monkeypatch):
    monkeypatch.delenv("LLMSTXT_MODEL", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    cfg = resolve_config(provider="deepseek")
    assert cfg.base_url == "https://api.deepseek.com"
    assert cfg.model == "deepseek-chat"
    assert cfg.api_key == "sk-ds"


def test_anthropic_not_openai_compatible(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    cfg = resolve_config(provider="anthropic")
    assert not cfg.openai_compatible


def test_args_override_env(monkeypatch):
    monkeypatch.setenv("LLMSTXT_MODEL", "from-env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = resolve_config(model="from-arg")
    assert cfg.model == "from-arg"


def test_unknown_provider_needs_base_url(monkeypatch):
    monkeypatch.delenv("LLMSTXT_BASE_URL", raising=False)
    with pytest.raises(ConfigError):
        resolve_config(provider="mystery")


def test_custom_openai_compatible(monkeypatch):
    monkeypatch.setenv("LLMSTXT_API_KEY", "k")
    cfg = resolve_config(provider="mystery", base_url="https://api.example.com/v1", model="m")
    assert cfg.openai_compatible
    assert cfg.base_url == "https://api.example.com/v1"
    assert cfg.model == "m"


def test_ollama_needs_no_key(monkeypatch):
    for var in ("LLMSTXT_API_KEY",):
        monkeypatch.delenv(var, raising=False)
    cfg = resolve_config(provider="ollama")
    assert cfg.api_key == "ollama"  # placeholder default
    assert cfg.base_url == "http://localhost:11434/v1"


def test_require_api_key_raises(monkeypatch):
    for var in ("OPENAI_API_KEY", "LLMSTXT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = resolve_config()
    with pytest.raises(ConfigError):
        cfg.require_api_key()


def test_overrides_validated(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(TypeError):
        resolve_config(not_a_real_field=123)


def test_estimate_cost_known_model():
    cost = estimate_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000)
    assert cost == round(0.15 + 0.60, 6)


def test_estimate_cost_unknown_model_returns_none():
    assert estimate_cost_usd("some-random-model", 1000, 1000) is None


def test_estimate_cost_override():
    cost = estimate_cost_usd("anything", 1_000_000, 0, price_in=2.0, price_out=8.0)
    assert cost == 2.0
