"""Impact agent (brownfield): reason about the REAL codebase.

It parses every ``shortener/*.py`` with ``ast`` into a function index and a
name-based call graph, then for each detected feature finds the call path from
the HTTP handler to the code that implements the rule. Calls are matched by
name only (``x.add()`` matches every ``add`` method), which is an
over-approximation: fine for "what might this touch", not a type checker.
"""
from __future__ import annotations

import ast
from collections import deque
from pathlib import Path
from typing import Optional

from orchestrator.models import AgentContext, AgentResult
from shortener.manifest import MANIFEST

from .common import features_of, shortener_root
from .features import FEATURES

Index = dict[str, dict]  # "module.Class.func" -> {"file", "line", "calls": set[str]}


def scan_package(root: Path) -> Index:
    index: Index = {}
    for path in sorted((root / "shortener").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = f"shortener/{path.name}"
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index[f"{path.stem}.{node.name}"] = _entry(node, rel)
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index[f"{path.stem}.{node.name}.{item.name}"] = _entry(item, rel)
    return index


def _entry(func: ast.AST, file: str) -> dict:
    calls: set[str] = set()
    for node in ast.walk(func):  # also covers decorators and default args, where Depends(...) lives
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if name:
            calls.add(name)
        if name == "Depends":  # FastAPI dependency: the argument is a function that will be called
            calls.update(a.id for a in node.args if isinstance(a, ast.Name))
    return {"file": file, "line": func.lineno, "calls": calls}


def find_path(index: Index, start: str, goal: str) -> Optional[list[str]]:
    """Shortest call path start -> goal (breadth-first), or None."""
    by_short: dict[str, list[str]] = {}
    for qualified in index:
        by_short.setdefault(qualified.rsplit(".", 1)[-1], []).append(qualified)
    queue, seen = deque([[start]]), {start}
    while queue:
        path = queue.popleft()
        if path[-1] == goal:
            return path
        for short in sorted(index[path[-1]]["calls"]):
            for nxt in by_short.get(short, []):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append([*path, nxt])
    return None


def _endpoint(handler: str) -> Optional[dict]:
    return next((e for e in MANIFEST["endpoints"] if e["handler"] == handler), None)


def impact_agent(ctx: AgentContext) -> AgentResult:
    root = shortener_root(ctx)
    index = scan_package(root)
    keys = features_of(ctx)

    touched: dict[str, dict] = {}       # module -> impacted_modules entry
    traces, support, risks, api_changes = [], [], [], []
    for key in keys:
        feature = FEATURES[key]
        missing = [t for t in feature.targets if t not in index]
        for target in feature.targets:
            if target in index:
                _touch(touched, index, target, key)
            for handler in feature.handlers:
                path = find_path(index, handler, target) if handler in index and target in index else None
                if path:
                    endpoint = _endpoint(handler)
                    prefix = f"{endpoint['method']} {endpoint['path']} -> " if endpoint else ""
                    traces.append({"feature": key, "trace": prefix + " -> ".join(path)})
                    for node in path:
                        _touch(touched, index, node, key)
        implemented = not missing and not feature.design_only
        support.append({"feature": key, "implemented": implemented, "design_only": feature.design_only})
        risks += [{"feature": key, "risk": r} for r in feature.risks]
        if feature.public_api:
            api_changes += [{"feature": key, "change": d, "breaking": False} for d in feature.api_deltas]

    modules = sorted(touched.values(), key=lambda m: m["module"])
    content = {
        "features": keys,
        "impacted_modules": modules,
        "endpoints": [{"method": e["method"], "path": e["path"], "handler": e["handler"]}
                      for e in MANIFEST["endpoints"] if any(e["handler"] in FEATURES[k].handlers for k in keys)],
        "data_fields": {f: MANIFEST["data_fields"][f] for k in keys for f in FEATURES[k].fields
                        if f in MANIFEST["data_fields"]},
        "call_traces": traces,
        "existing_support": support,
        "regression_risks": risks,
        "tests_to_rerun": _tests_to_rerun(root, [m["module"] for m in modules]),
        "public_api_changes": api_changes,
        "requires_approval": bool(api_changes),
        "approval_kind": "public_api_change",
        "scanned": {"functions": len(index), "files": len({e["file"] for e in index.values()})},
    }
    return AgentResult(artifacts={"impact": content},
                       summary=f"{len(modules)} module(s) impacted, {len(traces)} call trace(s), "
                               f"{len(api_changes)} public API change(s)")


def _touch(touched: dict[str, dict], index: Index, qualified: str, feature: str) -> None:
    module = qualified.split(".")[0]
    entry = touched.setdefault(module, {"module": module, "file": index[qualified]["file"],
                                        "functions": [], "features": []})
    if qualified not in entry["functions"]:
        entry["functions"].append(qualified)
    if feature not in entry["features"]:
        entry["features"].append(feature)


def _tests_to_rerun(root: Path, modules: list[str]) -> list[str]:
    """Test files that mention an impacted module (plus test_api, which drives everything over HTTP)."""
    found = []
    for path in sorted((root / "tests" / "shortener").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "test_api.py" or any(f"shortener.{m}" in text for m in modules):
            found.append(f"tests/shortener/{path.name}")
    return found


def api_change_review_agent(ctx: AgentContext) -> AgentResult:
    """Runs after a human approved the public API change: record exactly what was approved."""
    changes = ctx.artifacts.get("impact", {}).get("public_api_changes", [])
    content = {
        "approved_changes": changes,
        "breaking": any(c["breaking"] for c in changes),
        "backward_compatible": not any(c["breaking"] for c in changes),
        "reviewed_against": [d["change"] for d in ctx.artifacts.get("architecture", {}).get("api_deltas", [])],
    }
    return AgentResult(artifacts={"api_change_review": content},
                       summary=f"{len(changes)} public API change(s) reviewed, backward compatible: "
                               f"{content['backward_compatible']}")
