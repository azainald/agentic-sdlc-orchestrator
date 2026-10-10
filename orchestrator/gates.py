"""Quality gates: small pure functions ``GateContext -> GateResult``.

A gate is a *check on evidence* (artifacts, stage states, approvals), never an
action. Stages name the gates they need in ``entry_gates`` / ``exit_gates`` and
the engine looks them up in a registry. Unknown names fail closed.
"""
from __future__ import annotations

from typing import Optional

from .models import GateContext, GateFn, GateResult, StageState

BAD_STAGE_STATES = {
    StageState.FAILED.value,
    StageState.ROLLED_BACK.value,
    StageState.BLOCKED.value,
}


def _result(gate: str, passed: bool, ok_msg: str, fail_msg: str, **details) -> GateResult:
    return GateResult(gate=gate, passed=passed, message=ok_msg if passed else fail_msg, details=details)


def _missing(gate: str, name: str) -> GateResult:
    return GateResult(gate=gate, passed=False, message=f"artifact '{name}' is missing", details={"missing": name})


def requirements_complete(ctx: GateContext) -> GateResult:
    gate = "requirements_complete"
    art = ctx.artifacts.get("requirements")
    if art is None:
        return _missing(gate, "requirements")
    empty = [f for f in ("problem", "acceptance_criteria", "assumptions") if not art.get(f)]
    return _result(gate, not empty, "requirements are complete",
                   f"requirements missing or empty: {empty}", empty_fields=empty)


def ambiguity_resolved(ctx: GateContext) -> GateResult:
    """Every ``open_questions`` entry ({question, resolution}) needs a resolution."""
    gate = "ambiguity_resolved"
    art = ctx.artifacts.get("normalized_requirements") or ctx.artifacts.get("requirements")
    if art is None:
        return _missing(gate, "requirements")
    unresolved = [
        q.get("question", "?") if isinstance(q, dict) else str(q)
        for q in art.get("open_questions", [])
        if not (isinstance(q, dict) and str(q.get("resolution") or "").strip())
    ]
    return _result(gate, not unresolved, "all open questions are resolved",
                   f"{len(unresolved)} open question(s) unresolved", unresolved=unresolved)


def architecture_present(ctx: GateContext) -> GateResult:
    gate = "architecture_present"
    art = ctx.artifacts.get("architecture")
    if art is None:
        return _missing(gate, "architecture")
    n = len(art.get("components") or [])
    return _result(gate, n > 0, f"architecture defines {n} component(s)",
                   "architecture has no components", components=n)


def impact_mapped(ctx: GateContext) -> GateResult:
    gate = "impact_mapped"
    art = ctx.artifacts.get("impact")
    if art is None:
        return _missing(gate, "impact")
    n = len(art.get("impacted_modules") or [])
    return _result(gate, n > 0, f"impact analysis maps {n} module(s)",
                   "impact analysis lists no impacted modules", impacted_modules=n)


def implementation_complete(ctx: GateContext) -> GateResult:
    gate = "implementation_complete"
    art = ctx.artifacts.get("implementation")
    if art is None:
        return _missing(gate, "implementation")
    status = art.get("status")
    return _result(gate, status == "ok", "implementation status is ok",
                   f"implementation status is {status!r}, expected 'ok'", status=status)


def _tests_green(ctx: GateContext) -> tuple[bool, str, dict]:
    report = ctx.artifacts.get("test_report")
    if report is None:
        return False, "artifact 'test_report' is missing", {}
    passed, failed = report.get("passed", 0), report.get("failed", 0)
    details = {"passed": passed, "failed": failed}
    if failed != 0:
        return False, f"{failed} test(s) failed", details
    if passed <= 0:
        return False, "no tests passed (empty suite is not green)", details
    return True, f"{passed} test(s) passed, 0 failed", details


def tests_green(ctx: GateContext) -> GateResult:
    ok, msg, details = _tests_green(ctx)
    return GateResult(gate="tests_green", passed=ok, message=msg, details=details)


def docs_present(ctx: GateContext) -> GateResult:
    gate = "docs_present"
    art = ctx.artifacts.get("documentation")
    if art is None:
        return _missing(gate, "documentation")
    n = len(art.get("files") or [])
    return _result(gate, n > 0, f"documentation has {n} file(s)",
                   "documentation lists no files", files=n)


def release_ready(ctx: GateContext) -> GateResult:
    """Tests are green AND no other stage is failed / rolled back / blocked."""
    gate = "release_ready"
    ok, msg, details = _tests_green(ctx)
    bad = sorted(s for s, st in ctx.stage_states.items()
                 if st in BAD_STAGE_STATES and s != ctx.stage_id)
    if not ok:
        return GateResult(gate=gate, passed=False, message=f"not release ready: {msg}", details=details)
    if bad:
        return GateResult(gate=gate, passed=False,
                          message=f"not release ready: stage(s) in a bad state: {bad}",
                          details={**details, "bad_stages": bad})
    return GateResult(gate=gate, passed=True, message="tests green and no failed stages", details=details)


def no_policy_exceptions(ctx: GateContext) -> GateResult:
    """A requested security exception must be explicitly approved."""
    gate = "no_policy_exceptions"
    open_exceptions = [
        a.get("id") for a in ctx.approvals
        if a.get("kind") == "security_exception" and a.get("status") != "approved"
    ]
    return _result(gate, not open_exceptions, "no unapproved policy exceptions",
                   f"unapproved security exception(s): {open_exceptions}",
                   unapproved=open_exceptions)


GATES: dict[str, GateFn] = {
    "requirements_complete": requirements_complete,
    "ambiguity_resolved": ambiguity_resolved,
    "architecture_present": architecture_present,
    "impact_mapped": impact_mapped,
    "implementation_complete": implementation_complete,
    "tests_green": tests_green,
    "docs_present": docs_present,
    "release_ready": release_ready,
    "no_policy_exceptions": no_policy_exceptions,
}


def run_gates(names: list[str], ctx: GateContext, registry: Optional[dict[str, GateFn]] = None) -> list[GateResult]:
    """Evaluate gates in order. Fail closed: an unknown gate or a gate that
    raises counts as a failure, never as a pass."""
    registry = GATES if registry is None else registry
    results: list[GateResult] = []
    for name in names:
        fn = registry.get(name)
        if fn is None:
            results.append(GateResult(gate=name, passed=False, message=f"unknown gate '{name}' (fail closed)"))
            continue
        try:
            results.append(fn(ctx))
        except Exception as exc:  # a broken gate must not read as "passed"
            results.append(GateResult(gate=name, passed=False, message=f"gate raised {type(exc).__name__}: {exc}"))
    return results
