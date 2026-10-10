"""Interfaces between the engine and its collaborators.

The engine depends only on these Protocols, so persistence and telemetry can be
built and tested independently of it.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from .models import Run


class RunStore(Protocol):
    """Durable storage for runs. Saving after every state change lets a run
    resume after a restart (e.g. while waiting for approval)."""

    def save_run(self, run: Run) -> None: ...
    def load_run(self, run_id: str) -> Optional[Run]: ...
    def list_runs(self) -> list[Run]: ...


class TelemetrySink(Protocol):
    """Audit trace plus reliability metrics."""

    def emit(
        self,
        run_id: str,
        event: str,
        stage_id: Optional[str] = None,
        **details: Any,
    ) -> None: ...

    def run_started(self, run_id: str) -> None: ...

    def run_finished(self, run: Run) -> None:
        """Called once when a run reaches a terminal state. Records success/
        failure, retries, rollbacks, e2e latency and (for failed runs) MTTR."""
        ...

    def events_for(self, run_id: str) -> list[dict[str, Any]]: ...

    def snapshot(self) -> dict[str, Any]:
        """Aggregate view: runs_started, runs_succeeded, success_rate, retries,
        rollbacks, fallbacks, mttr_ms, avg_e2e_latency_ms, ..."""
        ...


class Orchestrator(Protocol):
    """Public surface of the engine (implemented in engine.py).

    ``run`` executes until the run is terminal OR paused waiting for approval,
    then returns. ``approve`` / ``reject`` mutate the saved state and resume by
    calling ``run`` again. Because state lives in ``Run`` (and the store), a
    paused run survives a process restart.
    """

    def create_run(
        self,
        scenario: str,
        requirement: str,
        graph: list,  # list[StageSpec]
        params: Optional[dict[str, Any]] = None,
    ) -> Run: ...

    async def run(self, run_id: str) -> Run: ...
    async def approve(self, run_id: str, approval_id: str, actor: str, comment: str = "") -> Run: ...
    async def reject(self, run_id: str, approval_id: str, actor: str, comment: str = "") -> Run: ...
    def stop(self, run_id: str) -> Run: ...
    def get_run(self, run_id: str) -> Optional[Run]: ...
