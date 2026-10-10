"""Plain-text (ASCII) rendering of a run for the CLI. No I/O here: functions return strings."""
from __future__ import annotations

import re
from typing import Any

from orchestrator.models import ApprovalStatus, Run, StageRuntime

# Control characters other than newline and tab. ESC (ANSI sequences), CR (overwrites a line), BS, BEL and the
# C1 range (0x9b is a one-byte CSI) let text that came from an artifact, a model or a requirement redraw or fake
# terminal output: the approver must see what is really there.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(text: Any) -> str:
    """Neutralise terminal control characters: each becomes a visible, inert escape such as ``\\x1b``."""
    return _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()), str(text))


def table(headers: list[str], rows: list[list[Any]]) -> str:
    headers = [safe(h) for h in headers]
    cells = [[safe(c) for c in row] for row in rows]
    widths = [max([len(h), *(len(r[i]) for r in cells)]) for i, h in enumerate(headers)]  # no rows: headers only
    line = lambda row: "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip()  # noqa: E731
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def duration_ms(st: StageRuntime) -> float | None:
    if st.started_at is None or st.ended_at is None:
        return None
    return (st.ended_at - st.started_at) * 1000


def gate_results(run: Run, stage_id: str) -> dict[str, dict[str, bool]]:
    """phase -> {gate: passed}, keeping the latest evaluation of each gate."""
    latest: dict[str, dict[str, bool]] = {"entry": {}, "exit": {}}
    for d in run.decisions:
        if d.kind == "gate" and d.stage_id == stage_id:
            latest[d.inputs["phase"]][d.inputs["gate"]] = d.outcome == "pass"
    return latest


def _gate_cell(results: dict[str, dict[str, bool]]) -> str:
    parts = [f"{phase} {sum(g.values())}/{len(g)}" for phase, g in results.items() if g]
    return ", ".join(parts) or "-"


def _approval_cell(run: Run, stage_id: str) -> str:
    items = []
    for a in run.approvals.values():
        if a.stage_id == stage_id:
            who = f" by {a.actor}" if a.actor else ""
            status = a.status.value
            if a.status == ApprovalStatus.REJECTED and a.actor == "engine":
                status = "withdrawn"  # cancelled by the engine (amendment / re-plan), not a human 'no'
            items.append(f"{a.kind}: {status}{who}")
    return "; ".join(items) or "-"


def stage_rows(run: Run) -> list[list[Any]]:
    """One row per stage. A RUNS column (executions, > 1 after a re-plan) appears only for re-planned runs, so the
    table of an ordinary run is unchanged."""
    replanned = any(st.runs > 1 for st in run.stages.values())
    rows = []
    for stage_id, st in run.stages.items():
        ms = duration_ms(st)
        tries = f"{st.attempts}" + (" (fallback)" if st.used_fallback else "")
        row = [stage_id, st.state.value, "-" if ms is None else f"{ms:.0f}", tries,
               _gate_cell(gate_results(run, stage_id)), _approval_cell(run, stage_id)]
        if replanned:
            row.insert(4, st.runs)
        rows.append(row)
    return rows


def stage_headers(run: Run) -> list[str]:
    headers = ["STAGE", "STATE", "MS", "ATTEMPTS", "GATES (passed/total)", "APPROVAL"]
    if any(st.runs > 1 for st in run.stages.values()):
        headers.insert(4, "RUNS")
    return headers


def pending_approvals(run: Run) -> list:
    return [a for a in run.approvals.values() if a.status == ApprovalStatus.PENDING]


def generators(run: Run) -> dict[str, str]:
    """artifact -> who wrote it ('llm:<model>', 'deterministic', 'deterministic (fallback)'), where it is recorded."""
    found = {}
    for name, art in run.artifacts.items():
        who = art.content.get("generator")
        if who:
            stage = run.stages.get(art.produced_by)
            found[name] = f"{who} (fallback)" if stage and stage.used_fallback else who
    return found


def llm_lines(run: Run) -> list[str]:
    """Banner and provenance, only for runs that opted in to the LLM layer (offline output is unchanged)."""
    if not run.params.get("llm"):
        return []
    who = ", ".join(f"{n}={g}" for n, g in generators(run).items()) or "none yet"
    return ["LLM layer: ON. LLM proposes; deterministic tools verify; gates and humans decide.",
            f"Generators: {who}"]


def format_run(run: Run) -> str:
    out = [f"Run {run.id}  scenario={run.scenario}  state={run.state.value.upper()}",
           f'Requirement: "{run.requirement}"', *llm_lines(run), "",
           table(stage_headers(run), stage_rows(run))]
    failures = [f"  {s.spec.id}: {s.error}" for s in run.stages.values() if s.error]
    if failures:
        out += ["", "Errors:", *failures]
    failed_gates = [f"  {d.stage_id} {d.inputs['phase']} {d.inputs['gate']}: {d.rationale}"
                    for d in run.decisions if d.kind == "gate" and d.outcome == "fail"]
    if failed_gates:
        out += ["", "Failed gate evaluations:", *failed_gates]
    out += ["", "Artifacts:", table(["NAME", "VER", "PRODUCED BY", "SHA256"],
                                    [[a.name, a.version, a.produced_by, a.sha256] for a in run.artifacts.values()])]
    out += ["", format_metrics(run)]
    return safe("\n".join(out))


def format_metrics(run: Run) -> str:
    m = run.metrics
    latency = "-" if m.e2e_latency_ms is None else f"{m.e2e_latency_ms:.0f} ms"
    return ("Metrics: " + ", ".join([f"e2e={latency}", f"stages_ok={m.stages_succeeded}",
                                     f"stages_failed={m.stages_failed}", f"retries={m.retries}",
                                     f"fallbacks={m.fallbacks}", f"rollbacks={m.rollbacks}",
                                     f"approvals={m.approvals_requested}", f"replans={m.replans}"]))


def format_trace(run: Run) -> str:
    rows = [[f"{(d.ts - run.created_at) * 1000:8.0f}", d.kind, d.stage_id or "-", d.outcome, d.rationale]
            for d in run.decisions]
    return table(["+MS", "KIND", "STAGE", "OUTCOME", "RATIONALE"], rows)


def review_lines(run: Run, approval) -> list[str]:
    """What the human is being asked to approve (the evidence behind the pause)."""
    arts = {n: a.content for n, a in run.artifacts.items()}
    if approval.kind == "requirement_interpretation":
        questions = arts.get("normalized_requirements", {}).get("open_questions", [])
        if not questions:
            return ["  NOTHING TO REVIEW - approving an empty interpretation (no question or proposal exists)"]
        # Everything the approval applies is shown: approving also ADDS the feature named in adds_feature.
        return [safe(f"  - {q['question']}\n      proposed: {q['proposed_resolution']}\n"
                     f"      adds feature: {q.get('adds_feature') or '(none)'}\n"
                     f"      rationale: {q.get('rationale', '')}")
                for q in questions]
    if approval.kind == "public_api_change":
        return [safe(f"  - [{c['feature']}] {c['change']}") for c in arts.get("impact", {}).get("public_api_changes", [])]
    if approval.kind == "production_release":
        rc = arts.get("test_report", {})
        return [safe(f"  - tests: {rc.get('passed', '?')} passed, {rc.get('failed', '?')} failed"),
                f"  - documentation: {len(arts.get('documentation', {}).get('files', []))} file(s)"]
    return []


def _path_flag(flag: str, path: str, default: str | None) -> str:
    """' --db <path>' unless it is the default (quoted when it contains spaces)."""
    if default is None or path == default:
        return ""
    return f' {flag} "{path}"' if " " in path else f" {flag} {path}"


def resume_hint(run: Run, db: str, default_db: str, audit: str | None = None,
                default_audit: str | None = None) -> str:
    """Tell the human exactly how to continue a paused run. A non-default --db and --audit are repeated in every
    command, otherwise the hint would point at the default store."""
    db_flag = _path_flag("--db", db, default_db) + _path_flag("--audit", audit or "", default_audit if audit else None)
    lines = []
    for a in pending_approvals(run):
        lines += [f"PAUSED: run {run.id} is waiting for approval '{a.id}' ({a.kind}) at stage '{a.stage_id}'.",
                  *review_lines(run, a),
                  f"  approve: python -m scenarios resume {run.id} --approve {a.id} --actor you{db_flag}",
                  f"  reject:  python -m scenarios resume {run.id} --reject {a.id} --actor you{db_flag}"]
    if lines:
        lines.append(f'  amend the requirement instead: python -m scenarios amend {run.id} --requirement "..." '
                     f"--actor you{db_flag}")
    return safe("\n".join(lines))


def run_summary(run: Run) -> dict[str, Any]:
    """JSON-friendly view for ``--json``."""
    summary = _summary(run)
    if run.params.get("llm"):
        summary["llm"] = {"enabled": True, "generators": generators(run)}
    return summary


def _summary(run: Run) -> dict[str, Any]:
    return {
        "run_id": run.id, "scenario": run.scenario, "state": run.state.value,
        "stages": [{"id": i, "state": s.state.value, "attempts": s.attempts, "used_fallback": s.used_fallback,
                    "duration_ms": duration_ms(s), "error": s.error} for i, s in run.stages.items()],
        "approvals": [a.model_dump(mode="json") for a in run.approvals.values()],
        "pending_approvals": [a.id for a in pending_approvals(run)],
        "artifacts": {n: {"version": a.version, "sha256": a.sha256, "produced_by": a.produced_by}
                      for n, a in run.artifacts.items()},
        "metrics": run.metrics.model_dump(),
    }
