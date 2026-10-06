"""Provider-agnostic LLM interface and the client agents use to call it."""

from __future__ import annotations

import abc
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError

from orchestrator.models import LLMUsage

M = TypeVar("M", bound=BaseModel)


class LLMRequest(BaseModel):
    role: str = Field(description="Agent role, e.g. Implementer")
    task: str = Field(description="Task kind within the role, e.g. implement")
    system: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict, description="Structured inputs")
    schema_name: str
    json_schema: dict[str, Any] = Field(default_factory=dict)
    max_output_tokens: int = 8000


class LLMResponse(BaseModel):
    content: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


class ProviderError(Exception):
    """Provider failed (network, auth, malformed output). Triggers fallback."""


class LLMProvider(abc.ABC):
    name: str = "provider"

    @abc.abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a JSON object answering ``request``."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) used for offline accounting and budgets."""
    return max(1, len(text) // 4)


UsageSink = Callable[[LLMUsage], None]


class LLMClient:
    """Structured-output facade: builds the request, validates against a Pydantic model,
    falls back to a secondary provider on failure, and reports usage to the engine."""

    def __init__(
        self,
        primary: LLMProvider,
        *,
        fallback: LLMProvider | None = None,
        on_usage: UsageSink | None = None,
        token_budget: Callable[[], int] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.on_usage = on_usage
        self.token_budget = token_budget
        self.fallback_events: list[str] = []

    async def structured(
        self,
        *,
        role: str,
        task: str,
        system: str,
        prompt: str,
        schema: type[M],
        context: dict[str, Any] | None = None,
    ) -> M:
        request = LLMRequest(
            role=role,
            task=task,
            system=system,
            prompt=prompt,
            context=context or {},
            schema_name=schema.__name__,
            json_schema=schema.model_json_schema(),
        )
        over_budget = (
            self.token_budget is not None
            and self.token_budget() < estimate_tokens(prompt) + request.max_output_tokens
        )
        if over_budget and self.fallback is not None:
            reason = "token budget nearly exhausted"
            return await self._call(self.fallback, request, schema, fallback_reason=reason)
        try:
            return await self._call(self.primary, request, schema)
        except (ProviderError, ValidationError) as exc:
            if self.fallback is None:
                raise
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            return await self._call(self.fallback, request, schema, fallback_reason=reason)

    async def _call(
        self,
        provider: LLMProvider,
        request: LLMRequest,
        schema: type[M],
        *,
        fallback_reason: str | None = None,
    ) -> M:
        if fallback_reason is not None:
            self.fallback_events.append(
                f"{request.role}/{request.task}: {self.primary.name} -> {provider.name} "
                f"({fallback_reason})"
            )
        started = time.perf_counter()
        response = await provider.complete(request)
        latency = time.perf_counter() - started
        if self.on_usage is not None:
            self.on_usage(
                LLMUsage(
                    provider=provider.name,
                    role=request.role,
                    task=request.task,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cost_usd=response.cost_usd,
                    latency_s=round(latency, 4),
                    fallback=fallback_reason is not None,
                )
            )
        try:
            return schema.model_validate(response.content)
        except ValidationError as exc:
            raise ProviderError(
                f"{provider.name} returned output that does not match {schema.__name__}: "
                f"{json.dumps(exc.errors(include_url=False), default=str)[:400]}"
            ) from exc
