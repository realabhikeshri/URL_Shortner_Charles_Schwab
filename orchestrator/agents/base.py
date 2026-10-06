"""Agent contract.

An agent receives an :class:`AgentContext` (its node, upstream artifacts, retry feedback, a
permission-scoped tool belt and an LLM client) and returns an :class:`AgentResult`.
Agents never write to disk themselves: they *propose* a ChangeSet that the engine applies
through the sandboxed workspace, after checking the agent holds the ``write`` permission.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from orchestrator.context import ContextStore
from orchestrator.models import AgentResult, RequirementSpec, TaskNode
from orchestrator.providers.base import LLMClient
from orchestrator.workspace import ToolBelt

READ = "read"
WRITE = "write"
RUN = "run"


@dataclass
class AgentContext:
    run_id: str
    node: TaskNode
    attempt: int
    context: ContextStore
    run_dir: Path
    feedback: list[str] = field(default_factory=list)
    tools: ToolBelt | None = None
    llm: LLMClient | None = None
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> RequirementSpec | None:
        return self.context.typed("spec", RequirementSpec)

    def require_tools(self) -> ToolBelt:
        if self.tools is None:
            raise RuntimeError(f"{self.node.id}: agent needs a workspace but none is attached")
        return self.tools

    def require_llm(self) -> LLMClient:
        if self.llm is None:
            raise RuntimeError(f"{self.node.id}: agent needs an LLM client but none is attached")
        return self.llm


class Agent(abc.ABC):
    name: ClassVar[str]
    permissions: ClassVar[frozenset[str]] = frozenset({READ})
    description: ClassVar[str] = ""

    @abc.abstractmethod
    async def run(self, ctx: AgentContext) -> AgentResult:
        """Execute one attempt of ``ctx.node``."""
