"""Audit trace and reliability metrics.

``Telemetry`` keeps a bounded in-memory event log (optionally mirrored to an
append-only JSONL audit file) and aggregates run-level reliability metrics.

MTTR (mean time to recovery), as defined here
---------------------------------------------
For a run that ended SUCCEEDED *despite* trouble, recovery time is::

    metrics.ended_at  -  ts of the run's first retry / fallback / rollback decision

i.e. "from the first sign a stage failed until the run finally recovered".
``mttr_ms`` is the mean of those samples. Runs that never needed recovery, and
runs that did not succeed, contribute no sample.

This is an approximation: the first recovery decision is recorded when the
engine *reacts* to a failure, a little after the failure itself, and it
measures recovery of the whole run (not of the single stage). The engine does
not store a separate "first failure at" timestamp, so this is the closest signal
available from ``Run.decisions``.
"""
from __future__ import annotations

import json
import math
import threading
from collections import deque
from typing import Any, Optional

from .models import Run, RunState, now

RECOVERY_KINDS = {"retry", "fallback", "rollback"}


class Telemetry:
    """Thread-safe TelemetrySink.

    ``max_events`` bounds memory (oldest events are evicted first).
    ``audit_path`` (None = disabled) appends every event as one JSON line.
    """

    def __init__(self, max_events: int = 5000, audit_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._audit = open(audit_path, "a", encoding="utf-8") if audit_path else None
        self._started_at: dict[str, float] = {}
        self._runs_started = 0
        self._outcomes = {s: 0 for s in
                          (RunState.SUCCEEDED, RunState.FAILED, RunState.STOPPED, RunState.ROLLED_BACK)}
        self._totals = {"retries": 0, "fallbacks": 0, "rollbacks": 0,
                        "approvals_requested": 0, "replans": 0}
        self._latencies_ms: list[float] = []
        self._mttr_ms: list[float] = []

    # ---- events ---------------------------------------------------------
    def emit(self, run_id: str, event: str, stage_id: Optional[str] = None, **details: Any) -> None:
        record = {"ts": now(), "run_id": run_id, "stage_id": stage_id, "event": event, "details": details}
        with self._lock:
            self._events.append(record)
            if self._audit:
                self._audit.write(json.dumps(record, default=str) + "\n")
                self._audit.flush()

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["run_id"] == run_id]

    # ---- run lifecycle --------------------------------------------------
    def run_started(self, run_id: str) -> None:
        with self._lock:
            self._runs_started += 1
            self._started_at[run_id] = now()

    def run_finished(self, run: Run) -> None:
        if run.state not in self._outcomes:
            return  # not terminal: nothing to classify
        m = run.metrics
        with self._lock:
            self._outcomes[run.state] += 1
            self._totals["retries"] += m.retries
            self._totals["fallbacks"] += m.fallbacks
            self._totals["rollbacks"] += m.rollbacks
            self._totals["approvals_requested"] += m.approvals_requested
            self._totals["replans"] += m.replans

            latency = m.e2e_latency_ms
            started = self._started_at.pop(run.id, None)
            if latency is None and started is not None:
                latency = (now() - started) * 1000
            if latency is not None:
                self._latencies_ms.append(latency)

            if run.state is RunState.SUCCEEDED and m.ended_at is not None:
                first = next((d.ts for d in run.decisions if d.kind in RECOVERY_KINDS), None)
                if first is not None:
                    self._mttr_ms.append(max(0.0, (m.ended_at - first) * 1000))

    # ---- aggregation ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            o, t = self._outcomes, self._totals
            finished = sum(o.values())
            lat = sorted(self._latencies_ms)
            return {
                "runs_started": self._runs_started,
                "runs_succeeded": o[RunState.SUCCEEDED],
                "runs_failed": o[RunState.FAILED],
                "runs_stopped": o[RunState.STOPPED],
                "runs_rolled_back": o[RunState.ROLLED_BACK],
                "success_rate": _ratio(o[RunState.SUCCEEDED], finished),
                "retries_total": t["retries"],
                "retry_rate_per_run": _ratio(t["retries"], finished),
                "fallbacks_total": t["fallbacks"],
                "rollbacks_total": t["rollbacks"],
                # share of finished runs that ended ROLLED_BACK
                "rollback_rate": _ratio(o[RunState.ROLLED_BACK], finished),
                "replans_total": t["replans"],
                "approvals_requested_total": t["approvals_requested"],
                "mttr_ms": _ratio(sum(self._mttr_ms), len(self._mttr_ms)),
                "avg_e2e_latency_ms": _ratio(sum(lat), len(lat)),
                "p95_e2e_latency_ms": _percentile(lat, 95),
                "generated_at": now(),
            }

    def close(self) -> None:
        with self._lock:
            if self._audit:
                self._audit.close()
                self._audit = None


class NullTelemetry:
    """No-op TelemetrySink for callers that don't want observability."""

    def emit(self, run_id: str, event: str, stage_id: Optional[str] = None, **details: Any) -> None:
        pass

    def run_started(self, run_id: str) -> None:
        pass

    def run_finished(self, run: Run) -> None:
        pass

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        return []

    def snapshot(self) -> dict[str, Any]:
        return {}


def to_prometheus_text(snapshot: dict[str, Any], prefix: str = "orchestrator_") -> str:
    """Render a snapshot as Prometheus exposition text (one gauge per metric).

    ``None`` values (no data yet) are omitted, as Prometheus has no null.
    """
    lines = []
    for key, value in snapshot.items():
        if value is None:
            continue
        name = prefix + key
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return numerator / denominator if denominator else None


def _percentile(sorted_values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_values:
        return None
    rank = math.ceil(pct / 100 * len(sorted_values))
    return sorted_values[max(rank, 1) - 1]
