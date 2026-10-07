"""LLM layer: client fallback, deterministic provider, Anthropic adapter, record/replay."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from orchestrator import dag
from orchestrator.models import LLMUsage, PlanDAG, RequirementSpec, RiskLevel, TaskNode
from orchestrator.providers.anthropic_provider import (
    AnthropicProvider,
    build_request,
    cost,
    parse_json,
)
from orchestrator.providers.base import (
    LLMClient,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
)
from orchestrator.providers.deterministic import DeterministicProvider
from orchestrator.providers.record_replay import RecordReplayProvider, fixture_key
from orchestrator.runner import bootstrap_nodes

ROOT = Path(__file__).resolve().parents[2]
GREENFIELD = (ROOT / "requirements" / "greenfield.yaml").read_text()
AMBIGUOUS = (ROOT / "requirements" / "ambiguous.txt").read_text()
AMENDMENT = "\nUpdate from security review: enterprise-ready requires API-key authentication on all write endpoints."


class Answer(BaseModel):
    value: int


class Scripted(LLMProvider):
    def __init__(self, name: str, content: dict[str, Any] | Exception) -> None:
        self.name = name
        self.content = content
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if isinstance(self.content, Exception):
            raise self.content
        return LLMResponse(content=self.content, input_tokens=10, output_tokens=5, cost_usd=0.01)


async def ask(client: LLMClient) -> Answer:
    return await client.structured(role="R", task="t", system="s", prompt="p", schema=Answer)


async def test_client_validates_and_reports_usage() -> None:
    usage: list[LLMUsage] = []
    client = LLMClient(Scripted("p", {"value": 3}), on_usage=usage.append)
    assert (await ask(client)).value == 3
    assert usage[0].provider == "p"
    assert usage[0].fallback is False
    assert usage[0].cost_usd == 0.01


@pytest.mark.parametrize("failure", [ProviderError("boom"), {"value": "not-an-int"}])
async def test_client_falls_back_on_error_or_invalid_output(failure: Any) -> None:
    usage: list[LLMUsage] = []
    fallback = Scripted("fallback", {"value": 7})
    client = LLMClient(Scripted("primary", failure), fallback=fallback, on_usage=usage.append)
    assert (await ask(client)).value == 7
    assert usage[-1].fallback is True
    assert usage[-1].provider == "fallback"
    assert "primary -> fallback" in client.fallback_events[0]


async def test_client_without_fallback_raises() -> None:
    with pytest.raises(ProviderError):
        await ask(LLMClient(Scripted("primary", ProviderError("down"))))


async def test_client_uses_fallback_when_token_budget_is_exhausted() -> None:
    primary = Scripted("primary", {"value": 1})
    client = LLMClient(primary, fallback=Scripted("cheap", {"value": 2}), token_budget=lambda: 10)
    assert (await ask(client)).value == 2
    assert primary.calls == 0
    assert "token budget" in client.fallback_events[0]


def det_client() -> LLMClient:
    return LLMClient(DeterministicProvider())


async def analyze(
    text: str, filename: str, previous: RequirementSpec | None = None
) -> RequirementSpec:
    return await det_client().structured(
        role="RequirementAnalyst",
        task="analyze",
        system="",
        prompt="",
        schema=RequirementSpec,
        context={
            "text": text,
            "filename": filename,
            "previous": previous.model_dump(mode="json") if previous else None,
        },
    )


async def test_deterministic_analysis_of_structured_yaml() -> None:
    spec = await analyze(GREENFIELD, "greenfield.yaml")
    assert spec.id == "REQ-GF-001"
    assert "core" in spec.capabilities
    assert len(spec.acceptance_criteria) == 8
    assert spec.ambiguity_score == 0.0
    assert spec.risk is RiskLevel.MEDIUM


async def test_deterministic_analysis_flags_ambiguity_and_resolves_it_on_amendment() -> None:
    v1 = await analyze(AMBIGUOUS, "ambiguous.txt")
    assert {a.term for a in v1.ambiguities} == {"enterprise-ready", "faster"}
    assert v1.ambiguity_score > 0.35
    assert "api_key_auth" not in v1.capabilities
    assert any(q.startswith("Q-1") for q in v1.open_questions)
    assert {"caching", "rate_limiting", "observability", "performance"} <= set(v1.capabilities)
    v2 = await analyze(AMBIGUOUS + AMENDMENT, "ambiguous.txt", previous=v1)
    assert v2.version == 2
    assert "api_key_auth" in v2.capabilities
    assert v2.risk is RiskLevel.HIGH
    assert not any(q.startswith("Q-1") for q in v2.open_questions)
    assert any("Resolved Q-1" in a for a in v2.assumptions)


async def test_deterministic_plan_is_a_valid_parallel_dag() -> None:
    spec = await analyze(GREENFIELD, "greenfield.yaml")
    plan = await det_client().structured(
        role="Planner",
        task="plan",
        system="",
        prompt="",
        schema=PlanDAG,
        context={"spec": spec.model_dump(mode="json")},
    )
    nodes = bootstrap_nodes() + [
        n if n.depends_on else n.model_copy(update={"depends_on": ["plan"]}) for n in plan.nodes
    ]
    dag.validate(nodes)
    layers = dag.generations(nodes)
    assert ["implement.codes", "implement.urls"] in layers  # parallel implement branch
    assert any(
        {"document.architecture", "test.integration", "test.properties"} <= set(layer)
        for layer in layers
    )
    assert nodes[-1].id == "release.readiness"


async def test_deterministic_delta_adds_auth_and_reruns_design() -> None:
    from orchestrator.models import DeltaPlan

    v1 = await analyze(AMBIGUOUS, "ambiguous.txt")
    v2 = await analyze(AMBIGUOUS + AMENDMENT, "ambiguous.txt", previous=v1)
    provider = DeterministicProvider()
    plan_v1 = provider.plan({"spec": v1.model_dump(mode="json")})
    current = bootstrap_nodes() + [
        n if n.depends_on else n.model_copy(update={"depends_on": ["plan"]}) for n in plan_v1.nodes
    ]
    delta = DeltaPlan.model_validate(
        provider.delta(
            {
                "old_spec": v1.model_dump(mode="json"),
                "new_spec": v2.model_dump(mode="json"),
                "nodes": [n.model_dump(mode="json") for n in current],
                "changed_nodes": [],
            }
        ).model_dump()
    )
    assert {n.id for n in delta.add} == {
        "implement.auth_migration",
        "implement.api_key_auth",
        "test.auth_security",
    }
    assert delta.rerun == ["design.enterprise"]
    assert "test.performance" in {m.id for m in delta.modify}
    dag.validate(dag.apply_delta(current, delta))
    no_change = provider.delta(
        {"old_spec": None, "new_spec": None, "nodes": [], "changed_nodes": ["x"]}
    )
    assert no_change.add == [] and "outside the" in no_change.rationale


def node_for(blueprint: str) -> dict[str, Any]:
    return TaskNode(
        id="n", type="implement", title="t", agent="Implementer", params={"blueprint": blueprint}
    ).model_dump(mode="json")


def test_fault_injected_opset_self_repairs_only_on_recognised_feedback() -> None:
    provider = DeterministicProvider()
    first = provider.change({"node": node_for("brownfield/alias_fix"), "feedback": []})
    assert "first attempt" in first.summary
    assert "code.lower()" in (first.ops[0].new or "")
    unknown = provider.change(
        {"node": node_for("brownfield/alias_fix"), "feedback": ["SyntaxError somewhere"]}
    )
    assert "not recognised" in unknown.summary
    repaired = provider.change(
        {
            "node": node_for("brownfield/alias_fix"),
            "feedback": ["FAILED test_generated_codes_are_case_sensitive"],
        }
    )
    assert "self-repair" in repaired.summary
    assert any("is_custom" in (op.new or "") for op in repaired.ops)
    with pytest.raises(ValueError, match="no blueprint"):
        provider.change(
            {
                "node": TaskNode(id="x", type="implement", title="t", agent="I").model_dump(
                    mode="json"
                )
            }
        )


async def test_unknown_role_is_a_provider_error() -> None:
    req = LLMRequest(role="Nobody", task="x", system="", prompt="", schema_name="X")
    with pytest.raises(ProviderError):
        await DeterministicProvider().complete(req)


# ----------------------------------------------------------------------------- anthropic


class FakeStream:
    def __init__(self, message: Any) -> None:
        self.message = message

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> Any:
        if isinstance(self.message, Exception):
            raise self.message
        return self.message


class FakeClient:
    def __init__(self, message: Any) -> None:
        self.kwargs: dict[str, Any] = {}
        self.message = message
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs: Any) -> FakeStream:
        self.kwargs = kwargs
        return FakeStream(self.message)


def message(text: str, stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        model="claude-opus-5-5",
    )


REQ = LLMRequest(
    role="Planner",
    task="plan",
    system="sys",
    prompt="do it",
    schema_name="Answer",
    json_schema=Answer.model_json_schema(),
)


async def test_anthropic_provider_request_shape_parsing_and_cost() -> None:
    fake = FakeClient(message('```json\n{"value": 5}\n```'))
    provider = AnthropicProvider(client=fake, effort="high")
    response = await provider.complete(REQ)
    assert response.content == {"value": 5}
    assert fake.kwargs["model"] == "claude-opus-5-5"
    assert fake.kwargs["output_config"] == {"effort": "high"}
    assert fake.kwargs["fallbacks"] == "default"
    assert "JSON schema" in fake.kwargs["system"]
    assert "thinking" not in fake.kwargs  # adaptive thinking is the model default
    assert response.cost_usd == cost("claude-opus-5-5", 1000, 500) == 0.014
    assert build_request(REQ, model="m", effort="low")["max_tokens"] >= 32000


@pytest.mark.parametrize(
    ("msg", "match"),
    [
        (message("{}", stop="refusal"), "refusal"),
        (message('{"value": 1', stop="max_tokens"), "truncated"),
        (message("no json here"), "no JSON"),
        (
            anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            "connection",
        ),
    ],
)
async def test_anthropic_provider_errors_become_provider_errors(msg: Any, match: str) -> None:
    with pytest.raises(ProviderError, match=match):
        await AnthropicProvider(client=FakeClient(msg)).complete(REQ)


def test_parse_json_edge_cases() -> None:
    assert parse_json('prefix {"a": 1} suffix') == {"a": 1}
    with pytest.raises(ProviderError):
        parse_json("{not json}")
    with pytest.raises(ProviderError):
        parse_json("[1, 2]")


# ----------------------------------------------------------------------------- record/replay


async def test_record_then_replay(tmp_path: Path) -> None:
    inner = Scripted("inner", {"value": 9})
    recorder = RecordReplayProvider(tmp_path, mode="record", inner=inner)
    request = LLMRequest(role="R", task="t", system="", prompt="took 2.31s", schema_name="Answer")
    await recorder.complete(request)
    assert recorder.path_for(request).exists()
    replay = RecordReplayProvider(tmp_path, mode="replay")
    same_but_slower = request.model_copy(update={"prompt": "took 9.99s"})
    assert fixture_key(request) == fixture_key(same_but_slower)
    assert (await replay.complete(same_but_slower)).content == {"value": 9}
    with pytest.raises(ProviderError, match="no recorded fixture"):
        await replay.complete(request.model_copy(update={"prompt": "different"}))
    with pytest.raises(ValueError, match="inner provider"):
        RecordReplayProvider(tmp_path, mode="record")


def test_committed_fixtures_are_valid_responses() -> None:
    files = list((ROOT / "fixtures").rglob("*.json"))
    assert len(files) >= 10
    import json

    for f in files:
        data = json.loads(f.read_text())
        LLMResponse.model_validate(data["response"])
        assert data["recorded_from"] in {"deterministic", "anthropic"}
