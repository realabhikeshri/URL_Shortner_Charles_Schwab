"""Subprocess entry point for the crash/resume test: starts a run whose middle node hangs."""

import asyncio
import sys
from pathlib import Path

from orchestrator.engine import RunConfig
from tests.orchestrator.fakes import FakeAgent, Step, make_orchestrator, node


def main(tmp: Path) -> None:
    agent = FakeAgent({"b": [Step(sleep=60)]})
    nodes = [node("a", outputs=["x"]), node("b", ["a"], inputs=["x"]), node("c", ["b"])]
    config = RunConfig.model_validate({"retry": {"base_delay_s": 0, "max_delay_s": 0}})
    orch = make_orchestrator(tmp, nodes, agent, config=config)
    asyncio.run(orch.start())


if __name__ == "__main__":
    main(Path(sys.argv[1]))
