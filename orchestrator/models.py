"""Core data model for the orchestrator.

Everything here is a plain pydantic model so that a whole ``Run`` can be
serialised to JSON, persisted, and resumed after a restart (for example while
it is waiting for a human approval).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, field_validator


def now() -> float:
    """Wall-clock seconds. Single place to patch in tests."""
    return time.time()


def fingerprint(content: Any) -> str:
    """Stable 16-char SHA-256 of any JSON-serialisable content."""
    raw = json.dumps(content, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class StageState(str, Enum):
    PENDING = "pending"                    # dependencies not finished
    READY = "ready"                        # dependencies done, entry gates passed
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"  # paused for a human
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"                    # safe-stop, or upstream failed
    BLOCKED = "blocked"                    # entry gate failed


TERMINAL_STAGE_STATES = {
    StageState.SUCCEEDED,
    StageState.FAILED,
    StageState.ROLLED_BACK,
    StageState.SKIPPED,
    StageState.BLOCKED,
}


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"          # safe-stop requested by a human or by policy
    ROLLED_BACK = "rolled_back"


TERMINAL_RUN_STATES = {
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.STOPPED,
    RunState.ROLLED_BACK,
}


class Autonomy(str, Enum):
    FULL = "full"                  # agent acts alone
    SUPERVISED = "supervised"      # agent acts, humans can see/stop
    HUMAN_GATED = "human_gated"    # agent must wait for approval


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AutoApprovePolicy:
    """Opt-in, engine-level switch that lets the *engine* grant approvals itself (demos / tests only).

    It is deliberately NOT a run parameter: anything a client can put in ``run.params`` must never be able to
    bypass a human. ``kinds`` are the approval kinds that may be auto-approved ('*' = all)."""

    kinds: frozenset[str]
    actor: str = "auto-approver"

    def allows(self, kind: str) -> bool:
        return "*" in self.kinds or kind in self.kinds


class StageSpec(BaseModel):
    """Static, declarative description of one node of the workflow graph."""

    id: str
    title: str
    agent: str                                  # key into the agent registry
    depends_on: list[str] = Field(default_factory=list)
    join: str = "all"                           # "all" | "any" over depends_on
    entry_gates: list[str] = Field(default_factory=list)   # names in gate registry
    exit_gates: list[str] = Field(default_factory=list)
    max_attempts: int = 2                       # bounded retries (1 = no retry)
    backoff_ms: int = 50                        # base delay, doubles per attempt
    fallback_agent: Optional[str] = None        # tried once after retries are spent
    rollback_agent: Optional[str] = None        # undo, run when the stage finally fails
    requires_approval: bool = False
    approval_kind: Optional[str] = None         # e.g. "public_api_change", "production_release"
    autonomy: Autonomy = Autonomy.SUPERVISED
    parallel_group: Optional[str] = None        # informational label
    consumes: list[str] = Field(default_factory=list)  # artifact names read (drives re-plan)
    # Per-attempt wall-clock limit. A Python thread cannot be force-killed, so on timeout the attempt is
    # abandoned (counts as failed) but the worker thread may linger until the agent returns.
    timeout_ms: Optional[int] = Field(default=None, gt=0)

    @field_validator("approval_kind")
    @classmethod
    def _normalise_kind(cls, value: Optional[str]) -> Optional[str]:
        """'Schema_Change ' and 'schema_change' are the same kind (policy compares lower-case)."""
        return (value.strip().lower() or None) if value is not None else None


class StageRuntime(BaseModel):
    spec: StageSpec
    state: StageState = StageState.PENDING
    attempts: int = 0
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None
    used_fallback: bool = False
    input_hashes: dict[str, str] = Field(default_factory=dict)  # artifact -> hash seen at run time
    runs: int = 0                               # how many times (re-)executed, >1 after re-plan
    requirement_fp: str = ""                    # fingerprint of run.requirement when this stage last started


class Artifact(BaseModel):
    name: str
    content: dict[str, Any]
    sha256: str
    produced_by: str                            # stage id
    produced_at: float = Field(default_factory=now)
    version: int = 1


class DecisionRecord(BaseModel):
    """One line of decision lineage: what was decided, by what, and why."""

    ts: float = Field(default_factory=now)
    run_id: str
    stage_id: Optional[str] = None
    # Every kind the engine emits:
    #   start     run created / started / resumed
    #   stage     stage lifecycle: started, succeeded, failed, skipped, blocked, requeued, recovered (after a restart)
    #   artifact  an artifact was published (sha256 + version)
    #   gate      one entry/exit gate evaluation (pass | fail)
    #   retry     a failed attempt of the primary agent, and the scheduled backoff
    #   fallback  the fallback agent was tried (attempted), and its failed attempt (failed)
    #   rollback  the rollback agent ran (applied | fail | skipped)
    #   approval  requested | approved | rejected | cancelled | auto-approved | paused
    #   policy    a hard-stop violation (stage fail), or a rejected requirement amendment
    #   stop      a safe-stop request and the stages it skipped
    #   replan    stale stages reset (applied), the cap tripped (limit_exceeded), or a human amended the
    #             requirement (requirement_amended)
    #   finish    the run reached a terminal state
    kind: str
    rationale: str
    outcome: str       # pass | fail | requested | approved | rejected | applied ...
    inputs: dict[str, Any] = Field(default_factory=dict)


class GateResult(BaseModel):
    gate: str
    passed: bool
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class Approval(BaseModel):
    id: str
    run_id: str
    stage_id: str
    kind: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: float = Field(default_factory=now)
    resolved_at: Optional[float] = None
    actor: Optional[str] = None
    comment: str = ""


class RunMetrics(BaseModel):
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    e2e_latency_ms: Optional[float] = None
    stages_succeeded: int = 0
    stages_failed: int = 0
    retries: int = 0
    fallbacks: int = 0
    rollbacks: int = 0
    approvals_requested: int = 0
    replans: int = 0
    approval_wait_ms: float = 0.0               # wall time with at least one approval pending (humans, not agents)
    active_ms: Optional[float] = None           # e2e_latency_ms minus approval_wait_ms


class Run(BaseModel):
    id: str
    scenario: str
    requirement: str
    state: RunState = RunState.PENDING
    stages: dict[str, StageRuntime] = Field(default_factory=dict)
    artifacts: dict[str, Artifact] = Field(default_factory=dict)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    approvals: dict[str, Approval] = Field(default_factory=dict)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    params: dict[str, Any] = Field(default_factory=dict)   # e.g. {"inject_failure": "testing"}
    stop_requested: bool = False
    created_at: float = Field(default_factory=now)


# --------------------------------------------------------------------------
# Agent and gate call signatures
# --------------------------------------------------------------------------
class AgentContext(BaseModel):
    """What an agent is allowed to see. Read-only snapshot, not the live Run."""

    run_id: str
    stage_id: str
    scenario: str
    requirement: str
    attempt: int                                # 1-based
    is_fallback: bool = False
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)  # name -> content
    params: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Artifacts to publish (name -> content) and a one-line summary."""

    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    summary: str = ""


AgentFn = Callable[[AgentContext], AgentResult]


class GateContext(BaseModel):
    run_id: str
    stage_id: str
    scenario: str
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    stage_states: dict[str, str] = Field(default_factory=dict)  # stage id -> state value
    params: dict[str, Any] = Field(default_factory=dict)
    approvals: list[dict[str, Any]] = Field(default_factory=list)


GateFn = Callable[[GateContext], GateResult]
