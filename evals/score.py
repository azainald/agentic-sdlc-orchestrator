"""Scoring. Pure functions over what an agent returned: no model calls here, so they unit-test offline."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from orchestrator.gates import GATES
from orchestrator.llm import LLMInvalidOutput, LLMUnavailable
from orchestrator.models import AgentContext, AgentResult, GateContext
from orchestrator.policy import PolicyEngine, PolicyViolation

from .cases import Case


@dataclass
class Outcome:
    """One agent call, checked the way the engine would check it (agent -> policy -> exit gates)."""
    status: str = "ok"             # ok | invalid_output | unavailable | policy_violation | crashed
    ms: float = 0.0
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    gates: dict[str, bool] = field(default_factory=dict)
    error: str = ""

    @property
    def content(self) -> dict[str, Any]:
        return next(iter(self.artifacts.values()), {})


def run_checked(agent: Callable[[AgentContext], AgentResult], ctx: AgentContext, exit_gates: list[str],
                known: Optional[dict[str, dict]] = None) -> Outcome:
    """Call one agent, then apply the engine's checks. ``known`` are the artifacts already published."""
    start = time.perf_counter()
    out = Outcome()
    try:
        result = agent(ctx)
    except LLMInvalidOutput as exc:
        out.status, out.error = "invalid_output", str(exc)
    except LLMUnavailable as exc:
        out.status, out.error = "unavailable", str(exc)
    except Exception as exc:  # noqa: BLE001 - an eval must keep going
        out.status, out.error = "crashed", f"{type(exc).__name__}: {exc}"[:200]
    else:
        try:
            for name, content in result.artifacts.items():
                PolicyEngine().check_artifact(name, content)
        except PolicyViolation as violation:
            out.status, out.error = "policy_violation", violation.message
        else:
            out.artifacts = result.artifacts
            gate_ctx = GateContext(run_id="eval", stage_id=ctx.stage_id, scenario="eval",
                                   artifacts={**(known or {}), **result.artifacts})
            out.gates = {g: GATES[g](gate_ctx).passed for g in exit_gates}
    out.ms = (time.perf_counter() - start) * 1000
    return out


def prf(expected: frozenset, got: set) -> tuple[float, float]:
    """Precision and recall. Nothing expected and nothing found is a perfect 1.0, not a division by zero."""
    hit = len(expected & got)
    precision = hit / len(got) if got else (1.0 if not expected else 0.0)
    recall = hit / len(expected) if expected else 1.0
    return precision, recall


def score_requirements(case: Case, out: Outcome) -> dict[str, Any]:
    ok = out.status == "ok"
    content = out.content
    features = set(content.get("detected_features", [])) if ok else set()
    precision, recall = prf(case.features, features) if ok else (0.0, 0.0)
    flagged = bool(content.get("open_questions")) if ok else None
    return {
        "schema_valid": out.status not in ("invalid_output", "crashed"),
        "gate_ok": ok and all(out.gates.values()),
        "precision": precision, "recall": recall,
        "vague_correct": ok and flagged == case.vague,
        "policy_ok": out.status != "policy_violation",
        "ms": out.ms, "error": out.error,
    }


def score_simple(out: Outcome) -> dict[str, Any]:
    """normalize / architecture / documentation: valid output, its gate (if any), policy, latency."""
    return {"schema_valid": out.status not in ("invalid_output", "crashed"),
            "gate_ok": out.status == "ok" and all(out.gates.values()),
            "policy_ok": out.status != "policy_violation", "ms": out.ms, "error": out.error}


def normalize_ok(out: Outcome) -> bool:
    """Proposals stay proposals: nothing resolved by the model, every question answered."""
    questions = out.content.get("open_questions", [])
    return out.status == "ok" and bool(questions) and all(
        q["resolution"] == "" and q["status"] == "proposed" and q["proposed_resolution"] for q in questions)


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the scored rows of one family and one mode."""
    n = len(rows)
    rate = lambda key: mean([1.0 if r[key] else 0.0 for r in rows])  # noqa: E731
    summary = {"n": n, "schema_valid": rate("schema_valid"), "gate_ok": rate("gate_ok"),
               "policy_violations": sum(not r["policy_ok"] for r in rows),
               "avg_ms": mean([r["ms"] for r in rows])}
    if rows and "precision" in rows[0]:
        summary.update(precision=mean([r["precision"] for r in rows]), recall=mean([r["recall"] for r in rows]),
                       vague_accuracy=rate("vague_correct"))
    return summary
