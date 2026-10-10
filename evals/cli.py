"""python -m evals [--llm] [--cases N] [--json] [--pause SECONDS]

Without --llm only the deterministic agents run (offline, no key). With --llm the configured model is
evaluated next to them, throttled for the free tier. Needs GEMINI_API_KEY (in .env or the environment);
LLM_ENABLED is not needed because --llm is the explicit opt-in."""
from __future__ import annotations

import argparse
import json
import sys

from orchestrator.agents import build_agent_registry
from orchestrator.llm import LLMUnavailable, build_client, get_settings

from .cases import CASES
from .runner import Throttled, evaluate, format_report, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__.split("\n\n")[0])
    parser.add_argument("--llm", action="store_true", help="also evaluate the LLM agents (network, needs a key)")
    parser.add_argument("--cases", type=int, default=len(CASES), help=f"run the first N of {len(CASES)} cases")
    parser.add_argument("--pause", type=float, default=5.0, help="seconds between model calls (free-tier limits)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    cases = CASES[:max(1, args.cases)]
    modes = {"deterministic": (build_agent_registry(), "")}
    if args.llm:
        settings = get_settings()
        try:
            client = Throttled(build_client(settings), args.pause)
        except LLMUnavailable as exc:
            print(f"cannot run --llm: {exc}", file=sys.stderr)
            return 2
        modes["llm"] = (build_agent_registry(llm=client), "_llm", client)
        print(f"LLM evals: model {settings.model} (+ fallbacks), {len(cases)} case(s), {args.pause:g}s between calls",
              file=sys.stderr)
    rows = evaluate(cases, modes)
    if args.json:
        print(json.dumps({"cases": rows, "summary": summary(rows)}, indent=2))
    else:
        print(format_report(rows))
    return 0
