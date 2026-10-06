import pytest

from orchestrator import dag
from orchestrator.models import DeltaPlan, NodeChange, NodeState, TaskType
from tests.orchestrator.fakes import node


def test_valid_diamond_has_expected_generations() -> None:
    nodes = [
        node("design", outputs=["design"]),
        node("impl", ["design"], inputs=["design"], outputs=["code"]),
        node("tests", ["impl"], inputs=["code"]),
        node("docs", ["impl"], inputs=["code"]),
        node("release", ["tests", "docs"], type_=TaskType.RELEASE_CHECK),
    ]
    dag.validate(nodes)
    assert dag.generations(nodes) == [["design"], ["impl"], ["docs", "tests"], ["release"]]


def test_cycle_is_rejected() -> None:
    nodes = [node("a", ["b"]), node("b", ["a"])]
    with pytest.raises(dag.DAGValidationError, match="cycle"):
        dag.validate(nodes)


def test_unknown_dependency_and_duplicates_are_rejected() -> None:
    with pytest.raises(dag.DAGValidationError, match="unknown dependencies"):
        dag.validate([node("a", ["ghost"])])
    with pytest.raises(dag.DAGValidationError, match="duplicate"):
        dag.validate([node("a"), node("a")])


def test_inputs_must_be_produced_upstream() -> None:
    nodes = [node("a", outputs=["x"]), node("b", ["a"], inputs=["x", "never-made"])]
    with pytest.raises(dag.DAGValidationError, match="never-made"):
        dag.validate(nodes)
    # an input produced by a *sibling* (not an ancestor) is also rejected
    sib = [node("root"), node("a", ["root"], outputs=["x"]), node("b", ["root"], inputs=["x"])]
    with pytest.raises(dag.DAGValidationError, match="inputs not produced upstream"):
        dag.validate(sib)
    dag.validate([node("a", inputs=["requirement"])])  # run input is always available


def test_orphans_and_dangling_work_are_rejected() -> None:
    with pytest.raises(dag.DAGValidationError, match="orphan"):
        dag.validate([node("a"), node("b")])
    nodes = [
        node("a"),
        node("b", ["a"]),
        node("dangling", ["a"]),
        node("release", ["b"], type_=TaskType.RELEASE_CHECK),
    ]
    with pytest.raises(dag.DAGValidationError, match="dangling"):
        dag.validate(nodes)


def test_parallel_nodes_with_overlapping_write_sets_are_rejected() -> None:
    nodes = [
        node("a"),
        node("x", ["a"], write_paths=["service/app.py"]),
        node("y", ["a"], write_paths=["service/**"]),
    ]
    with pytest.raises(dag.DAGValidationError, match="overlapping"):
        dag.validate(nodes)
    # serialising them makes it valid
    nodes[2] = node("y", ["x"], write_paths=["service/**"])
    dag.validate(nodes)


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ("service/app.py", "service/app.py", True),
        ("service/app.py", "service/**", True),
        ("service/a/*", "service/b/*", False),
        ("service/**", "service/api/*", True),
        ("tests/x.py", "docs/x.md", False),
    ],
)
def test_paths_overlap(a: str, b: str, overlap: bool) -> None:
    assert dag.paths_overlap(a, b) is overlap


def test_apply_delta_add_remove_modify() -> None:
    nodes = [node("a"), node("b", ["a"]), node("c", ["b"])]
    delta = DeltaPlan(
        add=[node("d", ["a"])],
        remove=["b"],
        modify=[NodeChange(id="c", changes={"title": "renamed"})],
    )
    out = {n.id: n for n in dag.apply_delta(nodes, delta)}
    assert set(out) == {"a", "c", "d"}
    assert out["c"].depends_on == []
    assert out["c"].title == "renamed"
    with pytest.raises(dag.DAGValidationError):
        dag.apply_delta(nodes, DeltaPlan(add=[node("a")]))
    with pytest.raises(dag.DAGValidationError):
        dag.apply_delta(nodes, DeltaPlan(modify=[NodeChange(id="zzz", changes={})]))


def test_mermaid_render_includes_edges_and_state_classes() -> None:
    nodes = [node("design.x"), node("impl-y", ["design.x"])]
    text = dag.to_mermaid(nodes, {"design.x": NodeState.SUCCEEDED, "impl-y": NodeState.FAILED})
    assert text.startswith("flowchart TD")
    assert "design_x --> impl_y" in text
    assert "class design_x ok;" in text
    assert "class impl_y bad;" in text
    assert dag.descendants(nodes, "design.x") == {"impl-y"}
