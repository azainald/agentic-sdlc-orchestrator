"""Run the agent families over the labelled cases and tabulate deterministic vs LLM."""
from __future__ import annotations

import tempfile
import time
from typing import Any, Callable, Optional

from orchestrator.llm import LLMClient, LLMRateLimited
from orchestrator.models import AgentContext, AgentFn
from scenarios.report import table

from .cases import Case
from .score import Outcome, normalize_ok, run_checked, score_requirements, score_simple, summarize

FAMILIES = ("requirements", "normalize", "architecture", "documentation")
EXIT_GATES = {"requirements": ["requirements_complete"], "normalize": [], "architecture": ["architecture_present"],
              "documentation": ["docs_present"]}


class Throttled:
    """Wraps a client for the free tier: a pause between calls, and one patient retry after a 429."""

    def __init__(self, client: LLMClient, pause_s: float, sleep: Callable[[float], None] = time.sleep):
        self._client, self._pause, self._sleep, self._last = client, pause_s, sleep, 0.0
        self.paused_s = 0.0          # time spent waiting for the rate limit, not the model: excluded from latency

    def _wait(self, seconds: float) -> None:
        self.paused_s += seconds
        self._sleep(seconds)

    @property
    def model(self) -> str:
        return self._client.model

    def generate_json(self, *args, **kwargs):
        wait = self._last + self._pause - time.monotonic()
        if wait > 0 and self._last:
            self._wait(wait)
        try:
            try:
                return self._client.generate_json(*args, **kwargs)
            except LLMRateLimited as exc:
                self._wait(min(exc.retry_after or 30.0, 60.0))
                return self._client.generate_json(*args, **kwargs)
        finally:
            self._last = time.monotonic()


def evaluate_case(case: Case, agents: dict[str, AgentFn], suffix: str, root: str,
                  throttle: Optional[Throttled] = None) -> dict[str, Any]:
    """One case through one agent family (``suffix`` '' = deterministic, '_llm' = model). Mirrors the pipeline:
    requirements, then normalize (if it asked questions) and architecture + documentation (if it found features)."""

    def ctx(stage: str, artifacts: dict) -> AgentContext:
        return AgentContext(run_id=f"eval-{case.id}", stage_id=stage, scenario="eval", requirement=case.text,
                            attempt=1, artifacts=artifacts, params={"shortener_root": root})

    def call(family: str, artifacts: dict) -> Outcome:
        paused = throttle.paused_s if throttle else 0.0
        out = run_checked(agents[family + suffix], ctx(family, artifacts), EXIT_GATES[family], artifacts)
        out.ms -= ((throttle.paused_s if throttle else 0.0) - paused) * 1000
        return out

    results: dict[str, Optional[dict]] = {f: None for f in FAMILIES}
    req = call("requirements", {})
    results["requirements"] = score_requirements(case, req)
    if req.status == "ok":
        known = {"requirements": req.content}
        if req.content.get("open_questions"):
            norm = call("normalize", known)
            results["normalize"] = {**score_simple(norm), "gate_ok": normalize_ok(norm)}
        if req.content.get("detected_features"):
            arch = call("architecture", known)
            results["architecture"] = score_simple(arch)
            if arch.status == "ok":
                docs = call("documentation", {**known, "architecture": arch.content, "implementation": {"parts": {}}})
                results["documentation"] = score_simple(docs)
    return {"case": case.id, "families": results}


def evaluate(cases: list[Case], modes: dict[str, tuple]) -> list[dict[str, Any]]:
    """``modes``: name -> (agent registry, agent-name suffix[, Throttled client]). One row per case and mode."""
    rows = []
    with tempfile.TemporaryDirectory() as root:
        for case in cases:
            for mode, (agents, suffix, *throttle) in modes.items():
                rows.append({"mode": mode, **evaluate_case(case, agents, suffix, root, *throttle)})
    return rows


def summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """family -> mode -> aggregate."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for family in FAMILIES:
        for mode in dict.fromkeys(r["mode"] for r in rows):
            scored = [r["families"][family] for r in rows if r["mode"] == mode and r["families"][family]]
            if scored:
                out.setdefault(family, {})[mode] = summarize(scored)
    return out


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def format_report(rows: list[dict[str, Any]]) -> str:
    per_case = []
    for r in rows:
        req = r["families"]["requirements"]
        downstream = " ".join(f"{f[:4]}=" + ("ok" if s["gate_ok"] and s["policy_ok"] else f"FAIL({s['error'][:40]})")
                              for f, s in r["families"].items() if f != "requirements" and s)
        per_case.append([r["case"], r["mode"], "yes" if req["schema_valid"] else "NO", "yes" if req["gate_ok"] else "NO",
                         f"{req['precision']:.2f}", f"{req['recall']:.2f}", "yes" if req["vague_correct"] else "NO",
                         "ok" if req["policy_ok"] else "VIOLATION", f"{req['ms']:.0f}",
                         downstream or "-", req["error"][:60]])
    out = ["Per case (requirements agent; downstream = normalize/architecture/documentation gate + policy)",
           table(["CASE", "MODE", "SCHEMA", "GATE", "PREC", "RECALL", "VAGUE", "POLICY", "MS", "DOWNSTREAM", "ERROR"],
                 per_case), "", "Summary per agent family"]
    agg = []
    for family, modes in summary(rows).items():
        for mode, s in modes.items():
            agg.append([family, mode, s["n"], _pct(s["schema_valid"]), _pct(s["gate_ok"]),
                        _pct(s["precision"]) if "precision" in s else "-", _pct(s["recall"]) if "recall" in s else "-",
                        _pct(s["vague_accuracy"]) if "vague_accuracy" in s else "-", s["policy_violations"],
                        f"{s['avg_ms']:.0f}"])
    out.append(table(["FAMILY", "MODE", "N", "SCHEMA", "GATE", "PREC", "RECALL", "VAGUE", "POLICY VIOL", "AVG MS"], agg))
    return "\n".join(out)
