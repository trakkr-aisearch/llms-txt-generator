"""Model-agnostic LLM client.

Two surfaces are needed by the pipeline:

  * ``complete()``  — a single blocking call (used for the optional "what does
                      the model know about this brand cold" prior). Run in a
                      thread by the builder.
  * ``stream()``    — an async token stream (used for the live compose).

Both work against any OpenAI-compatible Chat Completions endpoint via the
``openai`` SDK, and against Anthropic's native Messages API via the optional
``anthropic`` SDK. Cost estimation is best-effort and clearly approximate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Dict, Optional, Tuple

from .config import GeneratorConfig


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Completion:
    text: str
    usage: Usage


# Approximate public list prices, USD per million tokens (input, output).
# These move often — treat as a rough guide, not a billing source of truth.
# Override per run with LLMSTXT_PRICE_IN / LLMSTXT_PRICE_OUT (see cli/builder).
_APPROX_PRICING: Dict[str, Tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "deepseek-chat": (0.27, 1.10),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-3-5-haiku-latest": (0.80, 4.00),
}


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    price_in: Optional[float] = None,
    price_out: Optional[float] = None,
) -> Optional[float]:
    """Return an approximate USD cost, or ``None`` if the price is unknown.

    ``price_in`` / ``price_out`` are USD per million tokens and override the
    built-in table when supplied.
    """
    if price_in is None or price_out is None:
        rates = _APPROX_PRICING.get(model)
        if rates is None:
            return None
        if price_in is None:
            price_in = rates[0]
        if price_out is None:
            price_out = rates[1]
    return round(
        (input_tokens / 1_000_000) * price_in + (output_tokens / 1_000_000) * price_out,
        6,
    )


class LLMClient:
    """Thin wrapper over an OpenAI-compatible or Anthropic chat model."""

    def __init__(self, config: GeneratorConfig) -> None:
        self.config = config
        self.model = config.model
        self.api_key = config.require_api_key()
        self.base_url = config.base_url
        self.timeout = config.timeout
        self.extra_body = config.extra_body
        self.is_anthropic = not config.openai_compatible and config.provider == "anthropic"
        self.last_usage = Usage()

    # ── OpenAI-compatible ─────────────────────────────────────────────────────

    def _openai_kwargs(self) -> Dict:
        kwargs: Dict = {"api_key": self.api_key, "timeout": self.timeout}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

    def _complete_openai(self, system: str, user: str, *, max_tokens: int, temperature: float) -> Completion:
        from openai import OpenAI

        client = OpenAI(max_retries=0, **self._openai_kwargs())
        body: Dict = {}
        if self.extra_body:
            body["extra_body"] = self.extra_body
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                **body,
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"{type(e).__name__}: {e}") from e
        text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        return Completion(
            text=text,
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            ),
        )

    async def _stream_openai(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(max_retries=1, **self._openai_kwargs())
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        base: Dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if self.extra_body:
            base["extra_body"] = self.extra_body

        # Most providers report usage on the final chunk when asked; a few reject
        # the option, so fall back to a plain stream.
        try:
            stream = await client.chat.completions.create(
                stream_options={"include_usage": True}, **base
            )
        except Exception:
            stream = await client.chat.completions.create(**base)

        in_tok = out_tok = 0
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage:
                    in_tok = getattr(usage, "prompt_tokens", 0) or in_tok
                    out_tok = getattr(usage, "completion_tokens", 0) or out_tok
                if not chunk.choices:
                    continue
                piece = getattr(chunk.choices[0].delta, "content", None)
                if piece:
                    yield piece
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"{type(e).__name__}: {e}") from e
        self.last_usage = Usage(input_tokens=in_tok, output_tokens=out_tok)

    # ── Anthropic native ──────────────────────────────────────────────────────

    def _complete_anthropic(self, system: str, user: str, *, max_tokens: int, temperature: float) -> Completion:
        try:
            from anthropic import Anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("anthropic not installed — `pip install 'llmstxt-generator[anthropic]'`") from e

        client = Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=0)
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"{type(e).__name__}: {e}") from e
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return Completion(
            text=text,
            usage=Usage(
                input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
                output_tokens=getattr(resp.usage, "output_tokens", 0) or 0,
            ),
        )

    async def _stream_anthropic(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> AsyncIterator[str]:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("anthropic not installed — `pip install 'llmstxt-generator[anthropic]'`") from e

        client = AsyncAnthropic(api_key=self.api_key, timeout=self.timeout, max_retries=1)
        try:
            async with client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                async for piece in stream.text_stream:
                    if piece:
                        yield piece
                final = await stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"{type(e).__name__}: {e}") from e
        self.last_usage = Usage(
            input_tokens=getattr(final.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(final.usage, "output_tokens", 0) or 0,
        )

    # ── Public surface ────────────────────────────────────────────────────────

    def complete(self, system: str, user: str, *, max_tokens: int = 600, temperature: float = 0.2) -> Completion:
        if self.is_anthropic:
            return self._complete_anthropic(system, user, max_tokens=max_tokens, temperature=temperature)
        return self._complete_openai(system, user, max_tokens=max_tokens, temperature=temperature)

    def stream(self, system: str, user: str, *, max_tokens: int, temperature: float) -> AsyncIterator[str]:
        if self.is_anthropic:
            return self._stream_anthropic(system, user, max_tokens=max_tokens, temperature=temperature)
        return self._stream_openai(system, user, max_tokens=max_tokens, temperature=temperature)
