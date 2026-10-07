"""Record/replay provider: records real responses to ``fixtures/`` and replays them in tests.

Fixtures are keyed by sha256(role, task, schema, prompt). In ``replay`` mode a missing fixture is
a :class:`ProviderError` (so the client can fall back), never a silent network call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from orchestrator.hashing import sha256_json
from orchestrator.providers.base import LLMProvider, LLMRequest, LLMResponse, ProviderError

_VOLATILE = re.compile(r"\b\d+(?:\.\d+)?s\b")  # wall-clock durations such as "in 2.24s"


def fixture_key(request: LLMRequest) -> str:
    """Stable key: durations embedded in gate output are normalised so replays stay hermetic."""
    return sha256_json(
        {
            "role": request.role,
            "task": request.task,
            "schema": request.schema_name,
            "prompt": _VOLATILE.sub("<t>", request.prompt),
        }
    )[:24]


class RecordReplayProvider(LLMProvider):
    def __init__(
        self,
        directory: Path,
        *,
        mode: Literal["record", "replay"] = "replay",
        inner: LLMProvider | None = None,
    ) -> None:
        if mode == "record" and inner is None:
            raise ValueError("record mode needs an inner provider")
        self.directory = directory
        self.mode = mode
        self.inner = inner
        self.name = f"replay({inner.name})" if inner and mode == "record" else "replay"

    def path_for(self, request: LLMRequest) -> Path:
        return self.directory / request.role / f"{request.task}-{fixture_key(request)}.json"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        path = self.path_for(request)
        if self.mode == "replay":
            if not path.exists():
                raise ProviderError(f"no recorded fixture {path.relative_to(self.directory)}")
            data = json.loads(path.read_text(encoding="utf-8"))
            return LLMResponse.model_validate(data["response"])
        assert self.inner is not None  # noqa: S101 - guarded in __init__
        response = await self.inner.complete(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "recorded_from": self.inner.name,
                    "role": request.role,
                    "task": request.task,
                    "schema": request.schema_name,
                    "response": response.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return response
