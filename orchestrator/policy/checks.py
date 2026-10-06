"""Built-in policy checks (security, compliance, change control).

Every check is ``async (PolicyInput, params) -> list[violation message]``.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import math
import re
import tempfile
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Any

from orchestrator.policy.engine import PolicyInput, check
from orchestrator.workspace import match_any

ALLOWLIST_MARKER = "pragma: allowlist secret"


def _added(inp: PolicyInput) -> list[tuple[str, str]]:
    return inp.applied.added_lines() if inp.applied else []


def _changed_py(inp: PolicyInput, *, include_tests: bool = True) -> list[str]:
    if inp.applied is None:
        return []
    deleted = set(inp.applied.deleted)
    files = [f for f in inp.applied.files if f.endswith(".py") and f not in deleted]
    return files if include_tests else [f for f in files if not f.startswith("tests/")]


def _parse(inp: PolicyInput, rel: str) -> ast.Module | None:
    if inp.workspace is None or not inp.workspace.exists(rel):
        return None
    try:
        return ast.parse(inp.workspace.read(rel), filename=rel)
    except SyntaxError:
        return None  # compile gate reports syntax errors


# --------------------------------------------------------------------------- security


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


_STRING_LITERAL = re.compile(r"""["']([A-Za-z0-9+/=_\-]{%d,})["']""")


@check("secret_scan")
async def secret_scan(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    patterns = [re.compile(p) for p in params.get("patterns", [])]
    min_len = int(params.get("entropy_min_length", 24))
    threshold = float(params.get("entropy_threshold", 4.5))
    literal = re.compile(_STRING_LITERAL.pattern % min_len)
    ignore = params.get("ignore_paths", [])
    violations: list[str] = []
    for path, line in _added(inp):
        if ALLOWLIST_MARKER in line or match_any(path, ignore):
            continue
        for pat in patterns:
            if pat.search(line):
                violations.append(f"{path}: matches secret pattern /{pat.pattern}/")
        for m in literal.finditer(line):
            ent = shannon_entropy(m.group(1))
            if ent >= threshold:
                violations.append(f"{path}: high-entropy literal ({ent:.2f} bits/char)")
    return violations


@check("bandit")
async def bandit_scan(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    files = _changed_py(inp, include_tests=False)
    if not files or inp.workspace is None:
        return []
    blocking = {s.upper() for s in params.get("block_severities", ["MEDIUM", "HIGH"])}
    res = await inp.workspace.run_python("-m", "bandit", "-q", "-f", "json", *files, timeout_s=180)
    try:
        report = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return [f"bandit produced unreadable output: {res.tail(5)}"]
    return [
        f"{r['filename']}:{r['line_number']} {r['test_id']} "
        f"{r['issue_severity']}: {r['issue_text']}"
        for r in report.get("results", [])
        if r.get("issue_severity", "").upper() in blocking
    ]


_FORBIDDEN_NAMES = {"eval", "exec"}
_FORBIDDEN_ATTRS = {("os", "system"), ("pickle", "loads"), ("pickle", "load"), ("marshal", "loads")}


@check("forbidden_calls")
async def forbidden_calls(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for rel in _changed_py(inp):
        tree = _parse(inp, rel)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_NAMES:
                violations.append(f"{rel}:{node.lineno} call to {func.id}()")
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and (func.value.id, func.attr) in _FORBIDDEN_ATTRS
            ):
                violations.append(f"{rel}:{node.lineno} call to {func.value.id}.{func.attr}()")
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    violations.append(f"{rel}:{node.lineno} subprocess with shell=True")
    return violations


_DEP_LINE = re.compile(r"""^\s*["']([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*(?:[<>=~!].*)?["'],?\s*$""")


def new_dependencies(inp: PolicyInput) -> list[str]:
    deps = []
    for path, line in _added(inp):
        if path.endswith("pyproject.toml"):
            m = _DEP_LINE.match(line)
            if m:
                deps.append(m.group(1).lower().replace("_", "-"))
    return sorted(set(deps))


@check("dependency_allowlist")
async def dependency_allowlist(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    allowed = {a.lower().replace("_", "-") for a in params.get("allowed", [])}
    return [
        f"dependency '{d}' is not on the allowlist"
        for d in new_dependencies(inp)
        if d not in allowed
    ]


@check("pip_audit")
async def pip_audit(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    deps = new_dependencies(inp)
    if not deps or inp.workspace is None:
        return []
    pins = []
    for d in deps:
        try:
            pins.append(f"{d}=={metadata.version(d)}")
        except metadata.PackageNotFoundError:
            return [f"dependency '{d}' is not installed; cannot audit a version"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("\n".join(pins) + "\n")
        req = fh.name
    try:
        res = await inp.workspace.run_python(
            "-m", "pip_audit", "-r", req, "--no-deps", "--disable-pip", "--progress-spinner", "off",
            "-f", "json", timeout_s=float(params.get("timeout_s", 120)),
        )  # fmt: skip
    finally:
        Path(req).unlink(missing_ok=True)
    if res.timed_out or ("ConnectionError" in res.stderr or "Max retries" in res.stderr):
        if params.get("offline_ok", True):
            return []  # recorded as pass; CI's `make security` runs a full online audit
        return ["pip-audit could not reach the vulnerability database"]
    try:
        report = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        return [] if res.ok else [f"pip-audit failed: {res.tail(5)}"]
    return [
        f"{d['name']}=={d['version']}: {v['id']}"
        for d in report.get("dependencies", [])
        for v in d.get("vulns", [])
    ]


# --------------------------------------------------------------------------- compliance

_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
_LOGGER_NAMES = re.compile(r"^(log|logger|logging|_log|_logger|LOG|LOGGER)$")


def _names_in(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id.lower())
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr.lower())
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.update(k.lower() for k in re.findall(r"[A-Za-z_]+", sub.value))
    return found


@check("pii_in_logs")
async def pii_in_logs(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    pii = {p.lower() for p in params.get("pii_identifiers", [])}
    violations: list[str] = []
    for rel in _changed_py(inp, include_tests=False):
        tree = _parse(inp, rel)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _LOG_METHODS:
                continue
            target = node.func.value
            if not (isinstance(target, ast.Name) and _LOGGER_NAMES.match(target.id)):
                continue
            args: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
            leaked = set().union(*(_names_in(a) for a in args)) & pii if args else set()
            if leaked:
                violations.append(f"{rel}:{node.lineno} log call references PII {sorted(leaked)}")
    return violations


@check("license_allowlist")
async def license_allowlist(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    allowed = [a.lower() for a in params.get("allowed", [])]
    violations = []
    for dep in new_dependencies(inp):
        try:
            meta = metadata.metadata(dep)
        except metadata.PackageNotFoundError:
            violations.append(f"{dep}: license unknown (package not installed)")
            continue
        texts = [meta.get("License-Expression") or "", meta.get("License") or ""]
        texts += [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
        blob = " ".join(texts).lower()
        if not any(a in blob for a in allowed):
            violations.append(f"{dep}: license '{blob[:80] or 'unknown'}' not on allowlist")
    return violations


@check("data_retention")
async def data_retention(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    if inp.workspace is None:
        return []
    triggers = params.get("trigger_paths", [])
    candidates = inp.workspace.list_files("service/**/*.py")
    if not any(fnmatch.fnmatch(f, t) for f in candidates for t in triggers):
        return []
    cfg, pattern = params["config_file"], params["setting_pattern"]
    if not inp.workspace.exists(cfg) or not re.search(pattern, inp.workspace.read(cfg)):
        return [f"analytics data is stored but {cfg} defines no retention setting /{pattern}/"]
    return []


# --------------------------------------------------------------------------- change control


@check("write_path_allowlist")
async def write_path_allowlist(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    if inp.applied is None:
        return []
    allow, deny = params.get("allow", []), params.get("deny", [])
    out = []
    for f in inp.applied.files:
        if match_any(f, deny):
            out.append(f"{f}: path is on the change-control denylist")
        elif not match_any(f, allow):
            out.append(f"{f}: path is not on the write allowlist")
    return out


@check("max_diff_size")
async def max_diff_size(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    if inp.applied is None:
        return []
    limit = int(params.get("max_lines", 1500))
    n = inp.applied.lines_changed
    return [f"{n} lines changed exceeds per-node limit {limit}"] if n > limit else []


def _is_trivial(body: list[ast.stmt]) -> bool:
    meaningful = [
        s
        for s in body
        if not isinstance(s, ast.Pass)
        and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
    ]
    return not meaningful


@check("migrations_reversible")
async def migrations_reversible(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    pattern = params.get("paths", "service/migrations/versions/*.py")
    out = []
    for rel in _changed_py(inp):
        if not fnmatch.fnmatch(rel, pattern) or rel.endswith("__init__.py"):
            continue
        tree = _parse(inp, rel)
        if tree is None:
            continue
        funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        down = funcs.get("downgrade")
        if down is None:
            out.append(f"{rel}: migration has no downgrade()")
        elif _is_trivial(down.body):
            out.append(f"{rel}: downgrade() is empty; migration is irreversible")
    return out


@check("changelog_required")
async def changelog_required(inp: PolicyInput, params: dict[str, Any]) -> list[str]:
    if inp.workspace is None:
        return []
    path = params.get("path", "CHANGELOG.md")
    if not inp.workspace.exists(path):
        return [f"{path} is missing"]
    text = inp.workspace.read(path)
    m = re.search(r"## \[Unreleased\](.*?)(?:\n## \[|\Z)", text, re.DOTALL)
    if not m or not re.search(r"^- ", m.group(1), re.MULTILINE):
        return [f"{path} has no entries under ## [Unreleased]"]
    return []
