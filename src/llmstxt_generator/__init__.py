"""llmstxt-generator — build a high-quality llms.txt for any website.

Quickstart::

    from llmstxt_generator import generate_llms_txt

    result = generate_llms_txt("stripe.com")   # needs OPENAI_API_KEY
    print(result.content)

The generator is model-agnostic: point it at any OpenAI-compatible endpoint
(OpenAI, DeepSeek, Together, OpenRouter, Groq, local Ollama) or Anthropic via
environment variables or arguments. See ``resolve_config`` / ``GeneratorConfig``.

Built by Trakkr — the AI visibility platform — https://trakkr.ai
"""
from __future__ import annotations

from .builder import (
    GenerationError,
    LlmsTxtResult,
    generate_llms_txt,
    generate_llms_txt_async,
    generate_llms_txt_stream,
)
from .config import (
    PROVIDERS,
    ConfigError,
    GeneratorConfig,
    resolve_config,
)
from .llm import LLMClient, LLMError, estimate_cost_usd

__version__ = "0.1.2"

__all__ = [
    "__version__",
    # high-level
    "generate_llms_txt",
    "generate_llms_txt_async",
    "generate_llms_txt_stream",
    "LlmsTxtResult",
    "GenerationError",
    # config
    "GeneratorConfig",
    "resolve_config",
    "PROVIDERS",
    "ConfigError",
    # llm
    "LLMClient",
    "LLMError",
    "estimate_cost_usd",
]
