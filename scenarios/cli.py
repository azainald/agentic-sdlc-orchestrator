"""Command line for the orchestrator.

    python -m scenarios greenfield --auto-approve
    python -m scenarios brownfield                 # pauses at the first approval
    python -m scenarios resume <run_id> --approve <approval_id> --actor you
    python -m scenarios amend <run_id> --requirement "..." --actor you   # dynamic re-plan, see docs/REPLAN.md
    python -m scenarios status|trace|metrics [run_id]

State lives in SQLite, so ``resume`` works from a different process than the one that started the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from orchestrator.agents import build_agent_registry
from orchestrator.engine import Engine
from orchestrator.graph import downstream
from orchestrator.models import TERMINAL_RUN_STATES, AutoApprovePolicy, Run, RunState, StageState
from orchestrator.policy import PolicyViolation
from orchestrator.store import SqliteRunStore
from orchestrator.telemetry import Telemetry, replay_runs

from .registry import SCENARIOS, get_scenario
from .report import format_metrics, format_run, format_trace, pending_approvals, resume_hint, run_summary, safe, table

DEFAULT_DB = "data/runs.db"
DEFAULT_AUDIT = "data/audit.jsonl"
FAILURES = ("testing", "testing_hard", "testing_all")
BAD_STATES = {RunState.FAILED, RunState.ROLLED_BACK, RunState.STOPPED}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=DEFAULT_DB, help=f"SQLite run store (default {DEFAULT_DB})")
    common.add_argument("--audit", default=DEFAULT_AUDIT, help=f"JSONL audit trail (default {DEFAULT_AUDIT})")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    parser = argparse.ArgumentParser(prog="python -m scenarios", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in SCENARIOS:
        p = sub.add_parser(name, parents=[common], help=f"run the {name} scenario")
        p.add_argument("--auto-approve", action="store_true", help="DEMO ONLY: the engine grants every approval itself, actor 'demo-auto-approver'")
        p.add_argument("--inject-failure", choices=FAILURES, help="make the testing stage fail (see docs)")
        p.add_argument("--skip-pytest", action="store_true", help="do not launch the pytest subprocess")
        p.add_argument("--requirement", help="override the scenario's requirement text")
        p.add_argument("--llm", action="store_true", help="use LLM-backed agents where available (needs an API key)")

    p = sub.add_parser("resume", parents=[common], help="resolve an approval and continue a paused run")
    p.add_argument("run_id")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--approve", metavar="APPROVAL_ID")
    group.add_argument("--reject", metavar="APPROVAL_ID")
    p.add_argument("--actor", default="cli-user")
    p.add_argument("--comment", default="")

    p = sub.add_parser("amend", parents=[common], help="change the requirement of an unfinished run and re-plan")
    p.add_argument("run_id")
    p.add_argument("--requirement", required=True, help="the new requirement text (replaces the current one)")
    p.add_argument("--actor", default="cli-user")
    p.add_argument("--comment", default="")

    for name, help_text in [("status", "show a run (or list runs)"), ("trace", "show the decision trace of a run"),
                            ("metrics", "show metrics of a run (or all runs)")]:
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("run_id", nargs=None if name == "trace" else "?")
    return parser


@contextmanager
def session(db: str, audit: str, auto_approve: bool = False) -> Iterator[tuple[Engine, SqliteRunStore, Telemetry]]:
    for path in (db, audit):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    store, telemetry = SqliteRunStore(db), Telemetry(audit_path=audit)
    # Demo only: the engine (not the run params) is what grants approvals without a human.
    demo = AutoApprovePolicy(kinds=frozenset({"*"}), actor="demo-auto-approver") if auto_approve else None
    engine = Engine(build_agent_registry(), store=store, telemetry=telemetry, auto_approve=demo)
    try:
        yield engine, store, telemetry
    finally:
        engine.close()
        telemetry.close()
        store.close()


def exit_code(run: Run) -> int:
    return 1 if run.state in BAD_STATES else 0  # succeeded and waiting-for-approval are both fine


def show_run(run: Run, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(run_summary(run), indent=2))
    else:
        print(format_run(run))
        if run.state not in TERMINAL_RUN_STATES:
            print("\n" + resume_hint(run, args.db, DEFAULT_DB, args.audit, DEFAULT_AUDIT))
    return exit_code(run)


def cmd_scenario(args: argparse.Namespace) -> int:
    # --llm forces the LLM agents (the deterministic agent stays the fallback); without it the settings decide
    # (LLM_ENABLED + a key), so a plain run stays deterministic by default.
    scenario = get_scenario(args.command, llm=True if args.llm else None)
    params = dict(scenario.params)
    params.update({k: v for k, v in {"inject_failure": args.inject_failure,
                                     "skip_pytest": args.skip_pytest or None,
                                     "llm": args.llm or None}.items() if v})
    if args.auto_approve:  # stderr with --json so the JSON on stdout stays parseable
        print("*** DEMO MODE: all approvals auto-granted (no human review) ***",
              file=sys.stderr if args.json else sys.stdout)
    with session(args.db, args.audit, auto_approve=args.auto_approve) as (engine, _, _):
        run = engine.create_run(scenario.name, args.requirement or scenario.requirement, scenario.graph, params)
        return show_run(asyncio.run(engine.run(run.id)), args)


def cmd_resume(args: argparse.Namespace) -> int:
    with session(args.db, args.audit) as (engine, _, _):
        run = _load(engine, args.run_id)
        if args.approve:
            run = asyncio.run(engine.approve(run.id, args.approve, args.actor, args.comment))
        elif args.reject:
            run = asyncio.run(engine.reject(run.id, args.reject, args.actor, args.comment))
        else:
            run = asyncio.run(engine.run(run.id))
        return show_run(run, args)


def cmd_amend(args: argparse.Namespace) -> int:
    """Change the requirement of an unfinished run; the engine re-plans and resumes it. The output shows which stages
    re-ran and which kept their results, and the decision lineage the amendment wrote."""
    with session(args.db, args.audit) as (engine, _, _):
        run = _load(engine, args.run_id)
        runs_before = {i: s.runs for i, s in run.stages.items()}
        old_requirement, mark = run.requirement, len(run.decisions)
        run = asyncio.run(engine.amend_requirement(run.id, args.requirement, args.actor, args.comment))
        rows = amendment_rows(run, runs_before)
        lineage = [d for d in run.decisions[mark:] if d.kind in ("replan", "approval", "policy")]
        if args.json:
            print(json.dumps({**run_summary(run), "amendment": {
                "actor": args.actor, "previous_requirement": old_requirement, "requirement": run.requirement,
                "stages": [dict(zip(("id", "state", "runs_before", "runs", "result"), r)) for r in rows],
                "decisions": [d.model_dump(mode="json") for d in lineage]}}, indent=2))
        else:
            print(format_amendment(run, old_requirement, args.actor, rows, lineage))
            if run.state not in TERMINAL_RUN_STATES:
                print("\n" + resume_hint(run, args.db, DEFAULT_DB, args.audit, DEFAULT_AUDIT))
        return exit_code(run)


def amendment_rows(run: Run, runs_before: dict[str, int]) -> list[list]:
    """[stage, state, runs before, runs now, result]: result says whether the amendment made it run again."""
    specs = [s.spec for s in run.stages.values()]
    open_stages = [i for i, s in run.stages.items() if s.state != StageState.SUCCEEDED]
    rows = []
    for stage_id, st in run.stages.items():
        before, delta = runs_before.get(stage_id, 0), st.runs - runs_before.get(stage_id, 0)
        if delta > 0:
            result = "re-ran" if delta == 1 else f"re-ran x{delta}"
        elif st.state == StageState.SUCCEEDED and any(stage_id in downstream(specs, o) for o in open_stages):
            result = "kept so far (upstream not finished)"
        elif st.state == StageState.SUCCEEDED:
            result = "kept (inputs unchanged)"
        elif st.state == StageState.WAITING_APPROVAL:
            result = "waiting for approval"
        elif st.state == StageState.PENDING and before > 0:
            result = "queued to re-run"
        elif st.state == StageState.PENDING:
            result = "not run yet"
        else:
            result = st.state.value
        rows.append([stage_id, st.state.value, before, st.runs, result])
    return rows


def format_amendment(run: Run, old_requirement: str, actor: str, rows: list[list], lineage: list) -> str:
    out = [f"Run {run.id}  scenario={run.scenario}  state={run.state.value.upper()}",
           f"Requirement amended by {actor}:", f'  before: "{old_requirement}"', f'  after:  "{run.requirement}"', "",
           "Stages (RUNS = executions before -> now; RESULT = what this amendment did):",
           table(["STAGE", "STATE", "RUNS", "RESULT"], [[r[0], r[1], f"{r[2]} -> {r[3]}", r[4]] for r in rows]),
           "", "Re-plan lineage (decisions written by this command):"]
    out += [f"  {d.kind:<8} {d.stage_id or '-':<18} {d.outcome:<20} {d.rationale}" for d in lineage]
    out += ["", format_metrics(run)]
    return safe("\n".join(out))


def cmd_status(args: argparse.Namespace) -> int:
    with session(args.db, args.audit) as (engine, store, _):
        if args.run_id:
            return show_run(_load(engine, args.run_id), args)
        runs = store.list_runs()
        rows = [[r.id, r.scenario, r.state.value, ", ".join(a.id for a in pending_approvals(r)) or "-"] for r in runs]
        print(json.dumps([run_summary(r) for r in runs], indent=2) if args.json
              else table(["RUN", "SCENARIO", "STATE", "PENDING APPROVALS"], rows))
        return 0


def cmd_trace(args: argparse.Namespace) -> int:
    with session(args.db, args.audit) as (engine, _, _):
        run = _load(engine, args.run_id)
        print(json.dumps([d.model_dump(mode="json") for d in run.decisions], indent=2) if args.json
              else format_trace(run))
        return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Metrics are rebuilt from the stored runs, so they survive restarts."""
    with session(args.db, args.audit) as (engine, store, telemetry):
        if args.run_id:
            run = _load(engine, args.run_id)
            print(json.dumps(run.metrics.model_dump(), indent=2) if args.json else format_metrics(run))
            return 0
        replay = Telemetry()
        replay_runs(replay, store.list_runs())  # ignores runs that are not finished yet
        snapshot = replay.snapshot()
        print(json.dumps(snapshot, indent=2) if args.json
              else table(["METRIC", "VALUE"], [[k, v] for k, v in snapshot.items()]))
        return 0


def _load(engine: Engine, run_id: str) -> Run:
    run = engine.get_run(run_id)
    if run is None:
        raise KeyError(f"unknown run '{run_id}'")
    return run


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"resume": cmd_resume, "amend": cmd_amend, "status": cmd_status, "trace": cmd_trace,
               "metrics": cmd_metrics}.get(args.command, cmd_scenario)
    try:
        return handler(args)
    except (KeyError, ValueError) as exc:  # unknown run, unknown / already resolved approval, bad amendment
        print(safe(f"error: {exc.args[0] if isinstance(exc, KeyError) else exc}"), file=sys.stderr)
        return 2
    except PolicyViolation as violation:  # a requirement or comment that contains a secret / unsafe URL
        print(safe(f"error: policy violation: {violation.message} (rule {violation.rule}); refused, nothing was "
                   "created or changed"), file=sys.stderr)
        return 2
