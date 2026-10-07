"""Static + runtime analysis of the target Python/FastAPI codebase.

* AST: module index, internal import graph (relative imports resolved), symbol index, and an
  AST fallback for FastAPI route decorators.
* Runtime (in a sandboxed subprocess, never in the orchestrator process): the real FastAPI route
  table from ``create_app().routes``, table/column metadata from ``Base.metadata`` and the
  settings fields. Importing target code in-process would execute untrusted code and clash with
  the orchestrator's own module cache.
* Test-to-module mapping via the transitive import closure of each test module.
* Impact analysis: seeds (files the change will modify) -> reverse-dependency closure ->
  impacted modules, routes, tables, tests to update, migrations needed, blast-radius score.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

import networkx as nx

from orchestrator.workspace import ToolBelt, Workspace

INTROSPECT = r"""
import json, sys
out = {"routes": [], "tables": {}, "settings": [], "openapi": None}
try:
    from fastapi.routing import APIRoute
    from service.app import create_app
    app = create_app()

    def walk(routes):
        # FastAPI >= 0.140 wraps included routers lazily; recurse into them.
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            elif getattr(r, "original_router", None) is not None:
                yield from walk(r.original_router.routes)
            elif getattr(r, "routes", None):
                yield from walk(r.routes)

    for r in walk(app.routes):
        for m in sorted(r.methods):
            out["routes"].append({"method": m, "path": r.path,
                "endpoint": f"{r.endpoint.__module__}.{r.endpoint.__name__}",
                "summary": r.summary or r.name.replace("_", " ")})
    if "--openapi" in sys.argv:
        out["openapi"] = app.openapi()
except Exception as exc:
    out["routes_error"] = repr(exc)
try:
    import service.models  # noqa: F401
    from service.db import Base
    for t in Base.metadata.sorted_tables:
        out["tables"][t.name] = [c.name for c in t.columns]
except Exception as exc:
    out["tables_error"] = repr(exc)
try:
    from service.config import Settings
    out["settings"] = sorted(Settings.model_fields)
except Exception as exc:
    out["settings_error"] = repr(exc)
print(json.dumps(out, default=str))
"""

HTTP_DECORATORS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class CodebaseFacts:
    modules: list[str] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)
    symbols: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    routes: list[dict[str, str]] = field(default_factory=list)
    route_source: str = "none"
    tables: dict[str, list[str]] = field(default_factory=dict)
    settings: list[str] = field(default_factory=list)
    tests: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve(current: str, node: ast.ImportFrom, is_pkg: bool) -> str | None:
    if node.level == 0:
        return node.module
    base = current.split(".")
    if not is_pkg:
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join([*base, node.module] if node.module else base)


def parse_module(
    rel: str, source: str
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, str]]]:
    tree = ast.parse(source, filename=rel)
    name = module_name(rel)
    is_pkg = rel.endswith("__init__.py")
    imports: list[str] = []
    symbols: list[dict[str, Any]] = []
    routes: list[dict[str, str]] = []
    prefix = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            target = _resolve(name, node, is_pkg)
            if target:
                imports.append(target)
                imports += [f"{target}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "APIRouter":
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = str(kw.value.value)
    for top in tree.body:
        if isinstance(top, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            kind = "class" if isinstance(top, ast.ClassDef) else "function"
            symbols.append({"name": top.name, "kind": kind, "line": top.lineno})
            if kind == "function":
                for deco in top.decorator_list:
                    if (
                        isinstance(deco, ast.Call)
                        and isinstance(deco.func, ast.Attribute)
                        and deco.func.attr in HTTP_DECORATORS
                        and deco.args
                        and isinstance(deco.args[0], ast.Constant)
                    ):
                        routes.append(
                            {
                                "method": deco.func.attr.upper(),
                                "path": prefix + str(deco.args[0].value),
                                "endpoint": f"{name}.{top.name}",
                                "summary": top.name.replace("_", " "),
                            }
                        )
            if kind == "class":
                for item in top.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        symbols.append(
                            {
                                "name": f"{top.name}.{item.name}",
                                "kind": "method",
                                "line": item.lineno,
                            }
                        )
    return imports, symbols, routes


def import_graph(facts: CodebaseFacts) -> nx.DiGraph:  # type: ignore[type-arg]
    """Edge a -> b means module a imports internal module b."""
    graph: nx.DiGraph = nx.DiGraph()  # type: ignore[type-arg]
    known = set(facts.modules) | set(facts.tests)
    graph.add_nodes_from(known)
    for mod, imps in {**facts.imports, **facts.tests}.items():
        for imp in imps:
            target = imp
            while target and target not in known:
                target = target.rpartition(".")[0]
            if target and target != mod:
                graph.add_edge(mod, target)
    return graph


async def analyze(workspace: Workspace | ToolBelt, *, runtime: bool = True) -> CodebaseFacts:
    facts = CodebaseFacts()
    ast_routes: list[dict[str, str]] = []
    for rel in workspace.list_files("service/**/*.py") + workspace.list_files("tests/**/*.py"):
        try:
            imports, symbols, routes = parse_module(rel, workspace.read(rel))
        except SyntaxError as exc:
            facts.errors.append(f"{rel}: {exc}")
            continue
        internal = sorted({i for i in imports if i.split(".")[0] in {"service", "tests"}})
        name = module_name(rel)
        if rel.startswith("tests/"):
            facts.tests[name] = internal
        else:
            facts.modules.append(name)
            facts.imports[name] = internal
            facts.symbols[name] = symbols
            ast_routes += routes
    facts.modules.sort()
    if facts.modules and runtime:
        res = await workspace.run_python("-c", INTROSPECT, timeout_s=120)
        try:
            data = json.loads(res.stdout.strip().splitlines()[-1]) if res.ok else {}
        except (json.JSONDecodeError, IndexError):
            data = {}
        if data.get("routes"):
            facts.routes, facts.route_source = data["routes"], "fastapi"
        facts.tables = data.get("tables", {})
        facts.settings = data.get("settings", [])
        facts.errors += [str(data[k]) for k in ("routes_error", "tables_error") if k in data]
        if not res.ok:
            facts.errors.append(f"runtime introspection failed: {res.tail(5)}")
    if not facts.routes and ast_routes:
        facts.routes, facts.route_source = ast_routes, "ast"
    return facts


def impact(
    facts: CodebaseFacts, seed_files: Iterable[str], *, migrations_needed: bool
) -> dict[str, Any]:
    graph = import_graph(facts)
    seeds = sorted({module_name(f) for f in seed_files if f.endswith(".py")} & set(facts.modules))
    impacted: set[str] = set(seeds)
    for seed in seeds:
        impacted |= {m for m in nx.ancestors(graph, seed) if m in facts.modules}
    endpoints_mod = {r["endpoint"].rpartition(".")[0] for r in facts.routes}
    impacted_routes = [
        f"{r['method']} {r['path']}"
        for r in facts.routes
        if r["endpoint"].rpartition(".")[0] in impacted
    ]
    tests_to_update = sorted(
        t for t in facts.tests if any(m in impacted for m in nx.descendants(graph, t))
    )
    tables = sorted(facts.tables) if "service.models" in impacted else []
    n_mod = max(len(facts.modules), 1)
    score = (
        0.5 * len(impacted) / n_mod
        + 0.2 * (len(impacted_routes) / max(len(facts.routes), 1))
        + 0.2 * (1.0 if migrations_needed else 0.0)
        + 0.1 * (len(tests_to_update) / max(len(facts.tests), 1))
    )
    return {
        "modules": facts.modules,
        "seeds": seeds,
        "impacted_modules": sorted(impacted),
        "impacted_routes": impacted_routes,
        "route_source": facts.route_source,
        "routed_modules": sorted(endpoints_mod),
        "tables": facts.tables,
        "tables_touched": tables,
        "migrations_needed": migrations_needed,
        "tests_to_update": tests_to_update,
        "blast_radius": round(min(score, 1.0), 3),
        "symbols_indexed": sum(len(v) for v in facts.symbols.values()),
        "errors": facts.errors,
    }
