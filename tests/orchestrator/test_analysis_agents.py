"""Codebase analysis on a real (blueprint-materialised) service, and agent helper logic."""

import sys
from pathlib import Path

import pytest

from orchestrator.agents.analysts import migrations_needed, seed_files
from orchestrator.agents.base import AgentContext
from orchestrator.agents.builders import _replace_block, merge_changelog, render_api_doc
from orchestrator.agents.contracts import DesignDecision, DesignDoc
from orchestrator.agents.governance import Reviewer
from orchestrator.agents.planning import render_design
from orchestrator.analysis import codebase
from orchestrator.blueprints import Blueprint
from orchestrator.context import ContextStore
from orchestrator.models import AcceptanceCriterion, ChangeSet, RequirementSpec
from orchestrator.providers.base import LLMClient, LLMProvider, LLMRequest, LLMResponse
from orchestrator.runner import init_scaffold
from orchestrator.state import StateStore
from orchestrator.workspace import ToolBelt, Workspace
from tests.orchestrator.fakes import node

GREENFIELD_OPSETS = [
    "greenfield/foundation",
    "greenfield/codes",
    "greenfield/urls",
    "greenfield/links_api",
]


@pytest.fixture(scope="module")
def service_ws(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    root = tmp_path_factory.mktemp("svc") / "ws"
    init_scaffold(root)
    ws = Workspace(root, python=sys.executable)
    bp = Blueprint()
    for name in GREENFIELD_OPSETS:
        ws.apply(ChangeSet(summary=name, ops=bp.opset(name).ops))
    return ws


async def test_analysis_extracts_routes_tables_symbols_and_test_map(service_ws: Workspace) -> None:
    facts = await codebase.analyze(service_ws)
    assert facts.route_source == "fastapi"
    routes = {f"{r['method']} {r['path']}" for r in facts.routes}
    assert {"POST /api/v1/links", "GET /{code}", "GET /healthz"} <= routes
    assert "links" in facts.tables
    assert "code" in facts.tables["links"]
    assert "redirect_status_code" in facts.settings
    assert any(s["name"] == "LinkService.resolve" for s in facts.symbols["service.links"])
    assert "service.repository" in facts.imports["service.links"]
    assert "tests.service.test_api_links" in facts.tests

    result = codebase.impact(facts, ["service/repository.py"], migrations_needed=False)
    assert "service.repository" in result["seeds"]
    assert {"service.links", "service.api.links", "service.api.redirect"} <= set(
        result["impacted_modules"]
    )
    assert "GET /{code}" in result["impacted_routes"]
    assert "tests.service.test_api_links" in result["tests_to_update"]
    assert 0 < result["blast_radius"] < 1
    assert result["tables_touched"] == []


async def test_ast_fallback_when_runtime_introspection_is_disabled(service_ws: Workspace) -> None:
    facts = await codebase.analyze(service_ws, runtime=False)
    assert facts.route_source == "ast"
    assert any(r["path"] == "/api/v1/links" and r["method"] == "POST" for r in facts.routes)


async def test_empty_workspace_has_no_impact(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, python=sys.executable)
    facts = await codebase.analyze(ws)
    assert facts.modules == []
    assert codebase.impact(facts, [], migrations_needed=False)["blast_radius"] == 0.0


async def test_seed_selection_and_migration_detection(service_ws: Workspace) -> None:
    facts = await codebase.analyze(service_ws)
    spec = RequirementSpec(
        id="R",
        title="t",
        goal="Add link expiry and fix alias lookup",
        functional=["Links accept expires_at; alias lookup case-insensitive in the repository"],
    )
    seeds = seed_files(spec, facts)
    assert "service/repository.py" in seeds
    assert migrations_needed(spec, facts)  # expires_at is not a column yet
    plain = spec.model_copy(update={"functional": ["Change the redirect status code"], "goal": "x"})
    assert not migrations_needed(plain, facts)


def test_codebase_parse_handles_relative_imports() -> None:
    imports, symbols, routes = codebase.parse_module(
        "service/api/x.py", "from ..links import LinkService\nfrom . import deps\n"
    )
    assert "service.links" in imports
    assert "service.api" in imports
    assert symbols == []
    assert routes == []


def test_merge_changelog_inserts_and_deduplicates() -> None:
    base = "# Changelog\n\n## [Unreleased]\n\n### Added\n- a\n\n## [0.1.0]\n- old\n"
    out = merge_changelog(
        base, {"Added": ["a", "b"], "Fixed": ["bug"], "Changed": [], "Security": []}
    )
    unreleased = out.split("## [0.1.0]")[0]
    assert unreleased.count("- a") == 1
    assert "- b" in unreleased
    assert "### Fixed\n- bug" in unreleased
    assert "## [0.1.0]\n- old" in out
    fresh = merge_changelog("", {"Added": ["x"]})
    assert fresh.startswith("# Changelog")
    assert "- x" in fresh


def test_render_helpers() -> None:
    spec = RequirementSpec(
        id="R-1",
        title="T",
        goal="g",
        functional=["f"],
        acceptance_criteria=[AcceptanceCriterion(id="AC-1", given="a", when="b", then="c")],
        open_questions=["Q?"],
    )
    doc = DesignDoc(
        title="Design",
        summary="s",
        components=["c1"],
        decisions=[DesignDecision(decision="d", rationale="r")],
        open_questions=["Q?"],
    )
    md = render_design(doc, spec)
    assert "| d | r |" in md
    assert "**AC-1** Given a" in md
    assert "## Open questions" in md
    api = render_api_doc(
        {
            "info": {"title": "API", "version": "1"},
            "paths": {
                "/x/{id}": {
                    "post": {
                        "summary": "make",
                        "parameters": [{"name": "id", "in": "path", "required": True}],
                        "requestBody": {
                            "content": {"application/json": {"schema": {"$ref": "#/c/s/Body"}}}
                        },
                        "responses": {"201": {}, "422": {}},
                    }
                }
            },
        }
    )
    assert "## `POST /x/{id}`" in api
    assert "Request body: `Body`" in api
    assert "`201`, `422`" in api
    assert _replace_block("", "data", "lc").count("<!-- lc:start -->") == 1
    twice = _replace_block(_replace_block("# P\n", "one", "lc"), "two", "lc")
    assert "two" in twice
    assert "one" not in twice


class AlwaysApprove(LLMProvider):
    name = "yes-man"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content={"verdict": "approve", "summary": "looks great", "findings": [], "blocking": []}
        )


async def test_reviewer_cannot_approve_unverified_criteria(
    service_ws: Workspace, tmp_path: Path
) -> None:
    store = StateStore(tmp_path / "s.db")
    store.create_run(
        "r", scenario=None, requirement_path="r", workspace=str(service_ws.root), config={}
    )
    ctx_store = ContextStore(store, service_ws.root)
    spec = RequirementSpec(
        id="R",
        title="t",
        goal="g",
        functional=["f"],
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verified_by=[
                    "tests/service/test_codes.py::test_generated_codes_have_exact_length_and_alphabet"
                ],
            ),
            AcceptanceCriterion(
                id="AC-2",
                given="g",
                when="w",
                then="t",
                verified_by=["tests/service/test_codes.py::test_does_not_exist"],
            ),
            AcceptanceCriterion(
                id="AC-3", given="g", when="w", then="t", verified_by=["gate:p95_under_target"]
            ),
        ],
    )
    ctx_store.publish("spec", spec, node_id="intake")
    await service_ws.init_git()
    reviewer = Reviewer()
    ctx = AgentContext(
        run_id="r",
        node=node("review.code"),
        attempt=1,
        context=ctx_store,
        run_dir=tmp_path,
        tools=ToolBelt(service_ws, agent="Reviewer", permissions=reviewer.permissions),
        llm=LLMClient(AlwaysApprove()),
    )
    result = await reviewer.run(ctx)
    review = result.artifacts["review"]
    assert review["verdict"] == "request_changes"  # guardrail overrode the model
    verified = {c["id"]: c["verified"] for c in review["criteria"]}
    assert verified == {"AC-1": True, "AC-2": False, "AC-3": False}
