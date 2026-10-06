from pathlib import Path

from orchestrator.engine import RunConfig
from orchestrator.metrics import aggregate, collect_runs, run_metrics, to_prometheus
from orchestrator.models import LLMUsage, RunState, TaskType
from tests.orchestrator.fakes import FakeAgent, Step, make_orchestrator, node


async def test_run_metrics_capture_retries_mttr_latency_and_approvals(tmp_path: Path) -> None:
    agent = FakeAgent(
        {
            "impl": [Step(ok=False, sleep=0.05), Step(ok=True, sleep=0.05)],
            "docs": [Step(approval={"reasons": ["x"], "risk": "low"})],
        }
    )
    nodes = [
        node("impl"),
        node("docs", ["impl"], type_=TaskType.DOCUMENT),
        node("dead", ["impl"], max_attempts=1, optional=True),
    ]
    agent.script["dead"] = [Step(ok=False)]
    orch = make_orchestrator(
        tmp_path,
        nodes,
        agent,
        config=RunConfig.model_validate(
            {"retry": {"base_delay_s": 0, "max_delay_s": 0}, "auto_approve": "low"}
        ),
    )
    orch.store.add_llm_call("impl", LLMUsage(provider="p", role="r", task="t", input_tokens=10))
    orch.store.add_llm_call(
        "impl", LLMUsage(provider="d", role="r", task="t", output_tokens=5, fallback=True)
    )
    assert await orch.start() is RunState.SUCCEEDED

    m = run_metrics(orch.store)
    assert m["state"] == "SUCCEEDED"
    assert m["attempts"] == 4  # impl x2, docs, dead
    assert m["retries"] == 1
    assert m["retry_rate"] == 0.25
    assert m["nodes"]["SUCCEEDED"] == 2
    assert m["nodes"]["FAILED"] == 1
    assert m["node_success_rate"] == round(2 / 3, 4)
    assert m["recoveries"] == 1
    assert m["mttr_s"] is not None and m["mttr_s"] > 0
    assert m["unrecovered_failures"] == 1  # the optional node never recovered
    assert m["fallback_frequency"] == 0.5
    assert m["llm_input_tokens"] == 10 and m["llm_output_tokens"] == 5
    assert m["approvals"] == 1 and m["approvals_auto"] == 1
    assert m["approval_wait_mean_s"] is not None
    assert set(m["stage_latency_s"]) == {"implement", "document"}
    assert m["end_to_end_latency_s"] > 0

    runs = collect_runs(tmp_path / "runs")
    assert len(runs) == 1
    agg = aggregate(runs)
    assert agg["run_success_rate"] == 1.0
    assert agg["retry_rate"] == 0.25
    prom = to_prometheus(runs)
    assert "# TYPE orchestrator_retry_rate gauge" in prom
    assert 'orchestrator_retry_rate{run_id="run-test"' in prom
    assert 'orchestrator_stage_latency_s{run_id="run-test",stage="implement"}' in prom
    assert "orchestrator_run_success_rate 1.0" in prom


def test_aggregate_of_nothing() -> None:
    assert aggregate([]) == {"runs": 0}
