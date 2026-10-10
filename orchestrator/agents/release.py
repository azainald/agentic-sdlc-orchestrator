"""Release agents: a go/no-go checklist built from the run's evidence, and its rollback.

The checklist is also written to ``docs/generated/<run_id>/RELEASE_CHECKLIST.md``;
``rollback_release`` deletes that file, which is the one thing a release "does"."""
from __future__ import annotations

from orchestrator.models import AgentContext, AgentResult
from shortener.manifest import MANIFEST

from .common import effective_requirements, shortener_root
from .documentation import run_docs_dir

CHECKLIST_FILE = "RELEASE_CHECKLIST.md"

BASE_ROLLBACK_PLAN = [
    "Redeploy the previous container image / git tag of the shortener",
    "Short links are never hard-deleted (soft delete), so no data migration has to be reversed",
    "Re-run the smoke checks (health, create, redirect) against the restored version",
]


def _check(item: str, ok: bool, detail: str) -> dict:
    return {"item": item, "ok": ok, "detail": detail}


def release_agent(ctx: AgentContext) -> AgentResult:
    report = ctx.artifacts.get("test_report", {})
    docs = ctx.artifacts.get("documentation", {})
    impl = ctx.artifacts.get("implementation", {})
    req = effective_requirements(ctx)
    unresolved = [q["question"] for q in req.get("open_questions", []) if not q.get("resolution")]
    design_only = impl.get("design_only_parts", [])

    checks = [
        _check("tests green", report.get("failed", 1) == 0 and report.get("passed", 0) > 0,
               f"{report.get('passed', 0)} passed, {report.get('failed', '?')} failed"),
        _check("documentation present", bool(docs.get("files")), f"{len(docs.get('files', []))} file(s)"),
        _check("implementation verified", impl.get("status") == "ok", f"parts: {sorted(impl.get('parts', {}))}"),
        _check("no unresolved requirement questions", not unresolved, f"{len(unresolved)} unresolved"),
    ]
    approvals = ["production_release (this stage)"]
    if ctx.artifacts.get("impact", {}).get("requires_approval"):
        approvals.append("public_api_change (api_change_review)")
    if "normalized_requirements" in ctx.artifacts:
        approvals.append("requirement_interpretation")
    risks = [*(f"'{p}' is design only: nothing was built or executed" for p in design_only),
             *MANIFEST["limits"]["known_limitations"]]
    plan = [*BASE_ROLLBACK_PLAN, *(f"Disable the {p} job/hook if it was enabled" for p in design_only)]

    go = all(c["ok"] for c in checks)
    content = {"decision": "go" if go else "no-go", "checks": checks, "open_risks": risks,
               "rollback_plan": plan, "approvals_required": approvals}
    docs_dir = run_docs_dir(shortener_root(ctx), ctx.run_id)
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / CHECKLIST_FILE).write_text(_markdown(ctx, content), encoding="utf-8")
    if not go:
        failed = [c["item"] for c in checks if not c["ok"]]
        raise RuntimeError(f"release is no-go: {failed}")
    return AgentResult(artifacts={"release_checklist": content}, summary=f"go: {len(checks)} checks ok, {len(risks)} open risk(s)")


def _markdown(ctx: AgentContext, content: dict) -> str:
    lines = [f"# Release checklist ({ctx.run_id})", "", f"Decision: **{content['decision']}**", ""]
    lines += [f"- [{'x' if c['ok'] else ' '}] {c['item']}: {c['detail']}" for c in content["checks"]]
    lines += ["", "## Open risks", *(f"- {r}" for r in content["open_risks"]),
              "", "## Rollback plan", *(f"{i}. {s}" for i, s in enumerate(content["rollback_plan"], 1))]
    return "\n".join(lines) + "\n"


def rollback_release_agent(ctx: AgentContext) -> AgentResult:
    """Withdraw the release candidate: remove the checklist file, and say what was (not) undone."""
    path = run_docs_dir(shortener_root(ctx), ctx.run_id) / CHECKLIST_FILE
    existed = path.exists()
    path.unlink(missing_ok=True)
    undone = f"removed {CHECKLIST_FILE}" if existed else "no checklist file had been written"
    return AgentResult(summary=f"release rolled back ({ctx.params.get('failure')}): {undone}; nothing was deployed, "
                               "so no deployment had to be reverted")
