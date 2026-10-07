"""Claude via the official Anthropic SDK (used when ANTHROPIC_API_KEY is set).

* Model is configurable (default ``claude-opus-5-5``); thinking is adaptive (the model default),
  depth is controlled through ``output_config.effort``.
* Structured output: the JSON schema of the agent's contract is sent in the system prompt and the
  reply is parsed + validated with Pydantic by :class:`LLMClient`. A malformed reply raises
  :class:`ProviderError`, which triggers the deterministic fallback.
* Streaming with ``get_final_message()`` so large change sets never hit HTTP timeouts.
* Server-side refusal fallback is enabled by default (``fallbacks="default"``).
* Token and cost accounting from ``response.usage``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from orchestrator.providers.base import LLMProvider, LLMRequest, LLMResponse, ProviderError

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output). Override with ORCH_PRICE_IN / ORCH_PRICE_OUT.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def build_request(request: LLMRequest, *, model: str, effort: str) -> dict[str, Any]:
    """The exact keyword arguments sent to ``client.beta.messages.stream``."""
    system = (
        f"{request.system}\n\nRespond with a single JSON object (no prose, no code fences) that "
        f"validates against this JSON schema for {request.schema_name}:\n"
        f"{json.dumps(request.json_schema)}"
    )
    return {
        "model": model,
        "max_tokens": max(request.max_output_tokens, 32000),
        "system": system,
        "messages": [{"role": "user", "content": request.prompt}],
        "output_config": {"effort": effort},
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


def parse_json(text: str) -> dict[str, Any]:
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < 0:
        raise ProviderError("model reply contained no JSON object")
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(f"model reply was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProviderError("model reply JSON is not an object")
    return value


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
    price_in = float(os.environ.get("ORCH_PRICE_IN", price_in))
    price_out = float(os.environ.get("ORCH_PRICE_OUT", price_out))
    return round((input_tokens * price_in + output_tokens * price_out) / 1_000_000, 6)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str = "high",
        client: Any = None,
        timeout_s: float = 600.0,
    ) -> None:
        self.model = model or os.environ.get("ORCH_MODEL", DEFAULT_MODEL)
        self.effort = effort
        if client is None:
            import anthropic  # noqa: PLC0415 - optional at import time for offline use

            client = anthropic.AsyncAnthropic(timeout=timeout_s, max_retries=2)
        self.client = client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic  # noqa: PLC0415

        kwargs = build_request(request, model=self.model, effort=self.effort)
        try:
            async with self.client.beta.messages.stream(**kwargs) as stream:
                message = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"connection error: {exc}") from exc
        if message.stop_reason == "refusal":
            raise ProviderError("model declined the request (stop_reason=refusal)")
        if message.stop_reason == "max_tokens":
            raise ProviderError("model output truncated at max_tokens")
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        usage = message.usage
        return LLMResponse(
            content=parse_json(text),
            input_tokens=int(usage.input_tokens),
            output_tokens=int(usage.output_tokens),
            cost_usd=cost(self.model, int(usage.input_tokens), int(usage.output_tokens)),
            model=str(getattr(message, "model", self.model)),
        )
