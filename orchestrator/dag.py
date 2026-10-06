"""Plan DAG construction, validation, delta application and Mermaid rendering."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable

import networkx as nx

from orchestrator.models import DeltaPlan, NodeState, PlanDAG, TaskNode, TaskType

# Artifacts that exist before any node runs (provided by the run itself).
RUN_INPUTS = frozenset({"requirement"})


class DAGValidationError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def build_graph(nodes: Iterable[TaskNode]) -> nx.DiGraph:  # type: ignore[type-arg]
    graph: nx.DiGraph = nx.DiGraph()  # type: ignore[type-arg]
    node_list = list(nodes)
    for node in node_list:
        graph.add_node(node.id, task=node)
    for node in node_list:
        for dep in node.depends_on:
            graph.add_edge(dep, node.id)
    return graph


def _static_prefix(pattern: str) -> str:
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


def paths_overlap(a: str, b: str) -> bool:
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    pa, pb = _static_prefix(a), _static_prefix(b)
    wild_a, wild_b = pa != a, pb != b
    if wild_a and wild_b:
        return pa.startswith(pb) or pb.startswith(pa)
    return False


def validate(plan: PlanDAG | list[TaskNode], *, external_inputs: Iterable[str] = ()) -> nx.DiGraph:  # type: ignore[type-arg]
    """Validate a plan; return its graph or raise :class:`DAGValidationError`.

    Rules: unique ids, known dependencies, acyclic, every consumed artifact produced by an
    ancestor, weakly connected (no orphans), every node reaches a release_check when one
    exists, and nodes that may run concurrently have disjoint write sets.
    """
    nodes = plan.nodes if isinstance(plan, PlanDAG) else plan
    problems: list[str] = []
    ids = [n.id for n in nodes]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate node ids: {dupes}")
    known = set(ids)
    for n in nodes:
        missing = [d for d in n.depends_on if d not in known]
        if missing:
            problems.append(f"{n.id}: unknown dependencies {missing}")
    if problems:
        raise DAGValidationError(problems)

    graph = build_graph(nodes)
    if not nx.is_directed_acyclic_graph(graph):
        cycle = nx.find_cycle(graph)
        problems.append(f"cycle detected: {' -> '.join(e[0] for e in cycle)}")
        raise DAGValidationError(problems)

    available = set(RUN_INPUTS) | set(external_inputs)
    by_id = {n.id: n for n in nodes}
    for n in nodes:
        produced_upstream = set(available)
        for anc in nx.ancestors(graph, n.id):
            produced_upstream.update(by_id[anc].outputs)
        missing_inputs = [i for i in n.inputs if i not in produced_upstream]
        if missing_inputs:
            problems.append(f"{n.id}: inputs not produced upstream {missing_inputs}")

    if len(nodes) > 1 and not nx.is_weakly_connected(graph):
        comps = sorted(sorted(c) for c in nx.weakly_connected_components(graph))
        problems.append(f"orphan / disconnected nodes: {comps[1:]}")

    releases = [n.id for n in nodes if n.type is TaskType.RELEASE_CHECK]
    if releases:
        for n in nodes:
            if n.id in releases:
                continue
            if not any(nx.has_path(graph, n.id, r) for r in releases):
                problems.append(f"{n.id}: does not lead to a release_check (dangling work)")

    writers = [n for n in nodes if n.write_paths]
    for i, a in enumerate(writers):
        for b in writers[i + 1 :]:
            if nx.has_path(graph, a.id, b.id) or nx.has_path(graph, b.id, a.id):
                continue
            clash = [
                f"{pa}~{pb}"
                for pa in a.write_paths
                for pb in b.write_paths
                if paths_overlap(pa, pb)
            ]
            if clash:
                problems.append(
                    f"{a.id} and {b.id} may run in parallel but write overlapping paths {clash}"
                )

    if problems:
        raise DAGValidationError(problems)
    return graph


def generations(nodes: list[TaskNode]) -> list[list[str]]:
    """Topological layers: every node in layer k depends only on layers < k."""
    return [sorted(layer) for layer in nx.topological_generations(build_graph(nodes))]


def descendants(nodes: list[TaskNode], node_id: str) -> set[str]:
    graph = build_graph(nodes)
    return set(nx.descendants(graph, node_id)) if node_id in graph else set()


def apply_delta(nodes: list[TaskNode], delta: DeltaPlan) -> list[TaskNode]:
    """Return a new node list with ``delta`` applied (does not validate)."""
    by_id = {n.id: n for n in nodes}
    for node_id in delta.remove:
        by_id.pop(node_id, None)
    for change in delta.modify:
        if change.id not in by_id:
            raise DAGValidationError([f"modify: unknown node {change.id}"])
        current = by_id[change.id].model_dump()
        current.update(change.changes)
        by_id[change.id] = TaskNode.model_validate(current)
    for node in delta.add:
        if node.id in by_id:
            raise DAGValidationError([f"add: node {node.id} already exists"])
        by_id[node.id] = node
    removed = set(delta.remove)
    result = []
    for n in by_id.values():
        if removed & set(n.depends_on):
            kept = [d for d in n.depends_on if d not in removed]
            result.append(n.model_copy(update={"depends_on": kept}))
        else:
            result.append(n)
    return result


_STATE_CLASS = {
    NodeState.SUCCEEDED: "ok",
    NodeState.FAILED: "bad",
    NodeState.BLOCKED: "bad",
    NodeState.ROLLED_BACK: "warn",
    NodeState.RETRYING: "warn",
    NodeState.AWAITING_APPROVAL: "gate",
    NodeState.RUNNING: "run",
    NodeState.SKIPPED: "skip",
}


def to_mermaid(nodes: list[TaskNode], states: dict[str, NodeState] | None = None) -> str:
    states = states or {}
    lines = ["flowchart TD"]
    for n in nodes:
        label = f"{n.id}<br/><small>{n.type.value} · {n.agent} · risk {n.risk.value}</small>"
        lines.append(f'    {_mid(n.id)}["{label}"]')
    for n in nodes:
        for dep in n.depends_on:
            lines.append(f"    {_mid(dep)} --> {_mid(n.id)}")
    lines += [
        "    classDef ok fill:#d1fadf,stroke:#12b76a;",
        "    classDef bad fill:#fee4e2,stroke:#d92d20;",
        "    classDef warn fill:#fef0c7,stroke:#dc6803;",
        "    classDef gate fill:#e0eaff,stroke:#444ce7;",
        "    classDef run fill:#f2f4f7,stroke:#344054;",
        "    classDef skip fill:#f9fafb,stroke:#98a2b3,stroke-dasharray: 4 2;",
    ]
    for n in nodes:
        cls = _STATE_CLASS.get(states.get(n.id, NodeState.PENDING))
        if cls:
            lines.append(f"    class {_mid(n.id)} {cls};")
    return "\n".join(lines)


def _mid(node_id: str) -> str:
    return node_id.replace(".", "_").replace("-", "_").replace(":", "_")
