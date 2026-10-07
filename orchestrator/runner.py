"""Composition root: builds an Orchestrator for a run and manages scenario workspaces."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from orchestrator.agents.analysts import CodebaseAnalyst, RequirementAnalyst
from orchestrator.agents.base import Agent
from orchestrator.agents.builders import DocWriter, Implementer, TestEngineer
from orchestrator.agents.governance import ReleaseManager, Reviewer
from orchestrator.agents.planning import Architect, Planner
from orchestrator.approvals import AutonomyPolicy
from orchestrator.audit import verify_chain
from orchestrator.blueprints import Blueprint
from orchestrator.engine import EngineDeps, Orchestrator, Prompter, RunConfig
from orchestrator.metrics import run_metrics
from orchestrator.models import DeltaPlan, NodeState, RequirementSpec, TaskNode, TaskType
from orchestrator.policy.engine import PolicyEngine
from orchestrator.providers.base import LLMClient, LLMProvider
from orchestrator.providers.deterministic import DeterministicProvider
from orchestrator.state import StateStore
from orchestrator.workspace import Workspace

REPO_ROOT = Path(__file__).resolve().parent.parent
ProviderName = Literal["deterministic", "anthropic", "replay", "record"]


def bootstrap_nodes() -> list[TaskNode]:
    return [
        TaskNode(
            id="intake",
            type=TaskType.INTAKE,
            title="Normalise the requirement; detect ambiguity",
            agent="RequirementAnalyst",
            inputs=["requirement"],
            outputs=["spec"],
            exit_criteria=["has_output"],
            max_attempts=2,
        ),
        TaskNode(
            id="analysis.impact",
            type=TaskType.ANALYSIS,
            title="Codebase impact analysis (static + runtime introspection)",
            agent="CodebaseAnalyst",
            depends_on=["intake"],
            inputs=["spec"],
            outputs=["impact"],
            exit_criteria=["has_output"],
            max_attempts=2,
        ),
        TaskNode(
            id="plan",
            type=TaskType.PLAN,
            title="Decompose into a validated task DAG",
            agent="Planner",
            depends_on=["analysis.impact"],
            inputs=["spec", "impact"],
            outputs=["plan"],
            exit_criteria=["has_output"],
            max_attempts=3,
        ),
    ]


def make_agents() -> dict[str, Agent]:
    agents: list[Agent] = [
        RequirementAnalyst(),
        CodebaseAnalyst(),
        Planner(),
        Architect(),
        Implementer(),
        TestEngineer(),
        DocWriter(),
        Reviewer(),
        ReleaseManager(),
    ]
    return {a.name: a for a in agents}


def make_provider(
    name: ProviderName, *, model: str | None = None
) -> tuple[LLMProvider, LLMProvider]:
    """Return (primary, fallback). The fallback is always the deterministic provider."""
    from orchestrator.providers.record_replay import RecordReplayProvider  # noqa: PLC0415

    deterministic = DeterministicProvider()
    fixtures = REPO_ROOT / "fixtures"
    if name == "deterministic":
        return deterministic, deterministic
    if name == "replay":
        return RecordReplayProvider(fixtures, mode="replay"), deterministic
    from orchestrator.providers.anthropic_provider import AnthropicProvider  # noqa: PLC0415

    if name == "anthropic":
        return AnthropicProvider(model=model), deterministic
    inner: LLMProvider = (
        AnthropicProvider(model=model) if os.environ.get("ANTHROPIC_API_KEY") else deterministic
    )
    return RecordReplayProvider(fixtures, mode="record", inner=inner), deterministic


def default_provider() -> ProviderName:
    return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "deterministic"


class AgentReplanner:
    """Adapts RequirementAnalyst + Planner to the engine's Replanner protocol."""

    def __init__(self, analyst: RequirementAnalyst, planner: Planner, filename: str) -> None:
        self.analyst = analyst
        self.planner = planner
        self.filename = filename

    async def reanalyze(
        self, requirement_text: str, previous: RequirementSpec | None, llm: LLMClient | None
    ) -> RequirementSpec:
        if llm is None:
            raise RuntimeError("re-analysis needs an LLM client")
        return await self.analyst.analyze(requirement_text, self.filename, previous, llm)

    async def delta(
        self,
        *,
        old_spec: RequirementSpec | None,
        new_spec: RequirementSpec | None,
        nodes: list[TaskNode],
        states: dict[str, NodeState],
        changed_nodes: list[str],
        llm: LLMClient | None,
    ) -> DeltaPlan:
        if llm is None:
            raise RuntimeError("re-planning needs an LLM client")
        return await self.planner.delta(
            old_spec=old_spec,
            new_spec=new_spec,
            nodes=nodes,
            states=states,
            changed_nodes=changed_nodes,
            llm=llm,
        )


def build(
    run_dir: Path,
    *,
    workspace: Path,
    config: RunConfig,
    requirement_name: str,
    prompter: Prompter | None = None,
    policies_dir: Path | None = None,
) -> Orchestrator:
    from orchestrator.report import write_reports  # noqa: PLC0415 - avoid import cycle

    policies = policies_dir or REPO_ROOT / "policies"
    agents = make_agents()
    primary, fallback = make_provider(config.provider, model=config.model)  # type: ignore[arg-type]
    replanner = AgentReplanner(
        agents["RequirementAnalyst"],  # type: ignore[arg-type]
        agents["Planner"],  # type: ignore[arg-type]
        requirement_name,
    )
    deps = EngineDeps(
        agents=agents,
        workspace=Workspace(workspace, python=sys.executable),
        policy_engine=PolicyEngine.from_dir(policies),
        autonomy=AutonomyPolicy.load(policies / "autonomy.yaml"),
        llm=LLMClient(primary, fallback=fallback if primary is not fallback else None),
        replanner=replanner,
        prompter=prompter,
        finalizers=[write_reports],
    )
    return Orchestrator(run_dir, deps, config=config)


def open_run(run_dir: Path, *, prompter: Prompter | None = None) -> Orchestrator:
    store = StateStore(run_dir / "state.db")
    run = store.run()
    store.close()
    config = RunConfig.model_validate(run["config"])
    return build(
        run_dir,
        workspace=Path(run["workspace"]),
        config=config,
        requirement_name=Path(run["requirement_path"]).name,
        prompter=prompter,
    )


# ---------------------------------------------------------------------------- scenarios

SCENARIOS: dict[str, dict[str, str | None]] = {
    "greenfield": {"requirement": "requirements/greenfield.yaml", "after": None, "events": None},
    "brownfield": {
        "requirement": "requirements/brownfield.yaml",
        "after": "greenfield",
        "events": None,
    },
    "ambiguous": {
        "requirement": "requirements/ambiguous.txt",
        "after": "brownfield",
        "events": "requirements/ambiguous.events.yaml",
    },
}


def new_run_id(scenario: str) -> str:
    return f"{scenario}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"


def init_scaffold(workspace: Path) -> None:
    """A fresh greenfield workspace: pyproject (tool config, no deps), changelog, README."""
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    scaffold = Blueprint().scaffold_dir
    for src in (p for p in scaffold.iterdir() if p.is_file()):
        name = ".gitignore" if src.name == "gitignore" else src.name
        shutil.copyfile(src, workspace / name)


def copy_workspace(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(
            "__pycache__", ".mypy_cache", ".hypothesis", "*.db", ".coverage"
        ),
    )


@dataclass
class ScenarioResult:
    scenario: str
    run_id: str
    state: str
    run_dir: Path
    workspace: Path


def last_good_workspace(work_root: Path, scenario: str) -> Path | None:
    marker = work_root / scenario / ".orchestrator-scenario.json"
    if marker.exists() and json.loads(marker.read_text())["state"] == "SUCCEEDED":
        return work_root / scenario
    return None


def mark_workspace(workspace: Path, scenario: str, run_id: str, state: str) -> None:
    (workspace / ".orchestrator-scenario.json").write_text(
        json.dumps({"scenario": scenario, "run_id": run_id, "state": state}) + "\n"
    )


def run_metrics_summary(run_dir: Path) -> dict[str, Any]:
    store = StateStore(run_dir / "state.db")
    try:
        m = run_metrics(store)
    finally:
        store.close()
    m["audit_chain_ok"] = verify_chain(run_dir / "audit.jsonl").ok
    return m
