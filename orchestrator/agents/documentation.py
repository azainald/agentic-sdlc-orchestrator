"""Documentation agent: writes markdown under ``docs/generated/<run_id>/`` from the run's
artifacts and the product MANIFEST, then publishes the ``documentation`` artifact."""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from orchestrator.models import AgentContext, AgentResult
from shortener.manifest import MANIFEST

from .common import effective_requirements, shortener_root

GENERATED_DIR = Path("docs") / "generated"


def run_docs_dir(root: Path, run_id: str) -> Path:
    """The only place the agents write files. The run id is sanitised so it cannot escape the folder."""
    return root / GENERATED_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


def _bullets(items) -> str:
    return "\n".join(f"- {i}" for i in items) or "- (none)"


def changelog(ctx: AgentContext) -> str:
    req, impl = effective_requirements(ctx), ctx.artifacts.get("implementation", {})
    parts = impl.get("parts", {})
    verified = [f"{n}: {p['checks_ok']}/{p['checks']} checks passed against the real code"
                for n, p in parts.items() if p["verified"]]
    designed = [f"{n}: design only, not implemented" for n, p in parts.items() if p["design_only"]]
    return (f"# Changelog\n\n## Unreleased - {date.today().isoformat()} (run {ctx.run_id}, scenario {ctx.scenario})\n\n"
            f"### Request\n{ctx.requirement.strip()}\n\n### Problem\n{req.get('problem', '')}\n\n"
            f"### Verified\n{_bullets(verified)}\n\n### Design only\n{_bullets(designed)}\n\n"
            f"### Acceptance criteria\n{_bullets(req.get('acceptance_criteria', []))}\n")


def api_notes() -> str:
    rows = ["| Method | Path | Success | Errors | Notes |", "|---|---|---|---|---|"]
    for e in MANIFEST["endpoints"]:
        notes = "; ".join(filter(None, [e.get("notes"), "rate limited" if e.get("rate_limited") else "",
                                        "headers: " + ", ".join(e["headers"]) if e.get("headers") else ""]))
        rows.append(f"| {e['method']} | `{e['path']}` | {e['success']} | "
                    f"{', '.join(map(str, e['errors'])) or '-'} | {notes or '-'} |")
    codes = [f"| `{c}` | {s} |" for c, s in MANIFEST["error_codes"].items()]
    return ("# API notes\n\nGenerated from `shortener/manifest.py`.\n\n" + "\n".join(rows)
            + "\n\n## Error codes\n\n| Code | HTTP |\n|---|---|\n" + "\n".join(codes)
            + f"\n\n## Limits\n\n- {MANIFEST['limits']['rate_limit']}\n"
            + _bullets(MANIFEST["limits"]["known_limitations"]) + "\n")


def design_notes(ctx: AgentContext) -> str:
    arch = ctx.artifacts.get("architecture", {})
    adrs = [f"### {d['id']} {d['title']}\n- Context: {d['context']}\n- Decision: {d['decision']}\n"
            f"- Consequence: {d['consequence']}\n" for d in arch.get("decisions", [])]
    comps = [f"`{c['name']}` ({c['module']}): {c['responsibility']}" for c in arch.get("components", [])]
    return (f"# Design notes\n\n## Components\n{_bullets(comps)}\n\n## Data model changes\n"
            f"{_bullets(c['change'] for c in arch.get('data_model', []))}\n\n## API deltas\n"
            f"{_bullets(c['change'] for c in arch.get('api_deltas', []))}\n\n## Decisions\n\n" + "\n".join(adrs))


def documentation_agent(ctx: AgentContext) -> AgentResult:
    root = shortener_root(ctx)
    target = run_docs_dir(root, ctx.run_id)
    target.mkdir(parents=True, exist_ok=True)
    pages = {"CHANGELOG.md": changelog(ctx), "API.md": api_notes(), "DESIGN.md": design_notes(ctx)}
    files = []
    for name, text in pages.items():
        (target / name).write_text(text, encoding="utf-8")
        files.append({"path": (GENERATED_DIR / target.name / name).as_posix(), "bytes": len(text.encode("utf-8")),
                      "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]})
    endpoints = [f"{e['method']} {e['path']}" for e in MANIFEST["endpoints"]]
    content = {"files": files, "endpoints_documented": endpoints,
               "summary": f"{len(files)} markdown file(s) covering {len(endpoints)} endpoint(s) in docs/generated/{target.name}/",
               "generator": "deterministic"}
    return AgentResult(artifacts={"documentation": content}, summary=content["summary"])
