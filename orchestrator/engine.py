"""The orchestration engine.

How it works, in one paragraph: ``run()`` is a single asyncio loop. Each turn it
(1) works out which stages can start (dependencies met, entry gates pass,
approval granted), (2) launches them all as concurrent tasks, then (3) sleeps
on ``asyncio.wait(FIRST_COMPLETED)`` until some stage finishes (event-driven,
no polling). Agents are plain synchronous functions; they run in a thread pool
so parallel stages genuinely overlap, but they only *return* a result. Every
mutation of the ``Run`` happens on the event-loop thread, so no locks are
needed. When nothing is in flight and a stage is waiting for a human, ``run()``
saves the run and returns; ``approve()``/``reject()`` later resume it, even from
a brand-new ``Engine`` sharing the same store.

Known limitations (documented, not fixed):
* Resuming the same run from two processes at once is unsafe: the store has no compare-and-swap, so the
  last writer wins. Run one resumer per run (the HTTP API guards this inside a single process only).
* After a restart, a stage that was RUNNING is re-queued, and if it needs approval it asks for a NEW
  approval even though the earlier one was granted (see ``_recover_interrupted``).
* A Python thread cannot be killed, so a timed-out agent keeps running in its worker thread until it returns.
"""
from __future__ import annotations

import asyncio
import copy
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Optional

from .contracts import RunStore, TelemetrySink
from .gates import GATES, run_gates
from .graph import DEAD, READY, dependency_status, downstream, topological_order, validate_graph
from .models import (
    TERMINAL_RUN_STATES,
    TERMINAL_STAGE_STATES,
    AgentContext,
    AgentFn,
    AgentResult,
    Approval,
    ApprovalStatus,
    Artifact,
    AutoApprovePolicy,
    DecisionRecord,
    GateContext,
    GateFn,
    GateResult,
    Run,
    RunState,
    StageRuntime,
    StageSpec,
    StageState,
    fingerprint,
    now,
)
from .policy import HIGH_IMPACT, PolicyEngine, PolicyViolation, approval_kind_of, validate_human_actor

MAX_REPLANS = 3  # per source stage: a re-plan loop can never spin forever; hitting it FAILS the run


class _StopRequested(Exception):
    """Raised inside a stage when stop() arrives between attempts: the remaining attempts are skipped."""


class _MemoryStore:
    """Default store: keeps Run objects in a dict (not durable)."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def save_run(self, run: Run) -> None:
        self._runs[run.id] = run

    def load_run(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def list_runs(self) -> list[Run]:
        return list(self._runs.values())


class _NullTelemetry:
    """Default sink: drops everything."""

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


class Engine:
    """Implements the ``Orchestrator`` protocol from contracts.py."""

    def __init__(
        self,
        agents: dict[str, AgentFn],
        gates: Optional[dict[str, GateFn]] = None,
        policy: Optional[PolicyEngine] = None,
        store: Optional[RunStore] = None,
        telemetry: Optional[TelemetrySink] = None,
        max_workers: int = 6,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        auto_approve: Optional[AutoApprovePolicy] = None,
    ):
        """``auto_approve`` is the ONLY way approvals get granted without a human. It is a constructor
        argument, never a run parameter, so a client of the API cannot switch it on."""
        self.agents = agents
        self.gates = GATES if gates is None else gates  # a custom registry replaces, not extends
        self.policy = policy or PolicyEngine()
        self.store: RunStore = store if store is not None else _MemoryStore()
        self.telemetry: TelemetrySink = telemetry if telemetry is not None else _NullTelemetry()
        self._sleep = sleep
        self.auto_approve = auto_approve
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent")
        self._live: dict[str, Run] = {}            # runs whose run() loop is active in this engine
        self._wake: dict[str, asyncio.Event] = {}  # lets approve()/stop() interrupt the loop's wait

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    # ================================================================ public API
    def create_run(
        self,
        scenario: str,
        requirement: str,
        graph: list[StageSpec],
        params: Optional[dict[str, Any]] = None,
    ) -> Run:
        validate_graph(graph)
        for spec in graph:
            self.policy.check_stage(spec)  # PolicyViolation => run is never created
            for agent in (spec.agent, spec.fallback_agent, spec.rollback_agent):
                if agent is not None and agent not in self.agents:
                    raise ValueError(f"stage '{spec.id}' uses unregistered agent '{agent}'")
        run = Run(
            id=f"run-{uuid.uuid4().hex[:8]}",
            scenario=scenario,
            requirement=requirement,
            stages={s.id: StageRuntime(spec=s.model_copy(deep=True)) for s in graph},
            params=dict(params or {}),
        )
        self._record(run, "start", f"run created for scenario '{scenario}' with {len(graph)} stages", "created")
        self._save(run)
        return run

    async def run(self, run_id: str) -> Run:
        """Execute until terminal or paused for approval, then return."""
        if run_id in self._live:
            raise RuntimeError(f"run '{run_id}' is already executing")
        run = self._require(run_id)
        if run.state in TERMINAL_RUN_STATES:
            return run
        self._live[run_id] = run
        try:
            await self._drive(run)
        finally:  # no await between _settle() and here, so approve()/stop() never see a half-closed run
            self._live.pop(run_id, None)
            self._wake.pop(run_id, None)
        return run

    async def approve(self, run_id: str, approval_id: str, actor: str, comment: str = "") -> Run:
        return await self._resolve(run_id, approval_id, self._human(actor), comment, ApprovalStatus.APPROVED)

    async def reject(self, run_id: str, approval_id: str, actor: str, comment: str = "") -> Run:
        return await self._resolve(run_id, approval_id, self._human(actor), comment, ApprovalStatus.REJECTED)

    def _human(self, actor: str) -> str:
        """Human decisions need a real name; the engine's machine actors are reserved."""
        return validate_human_actor(actor, [self.auto_approve.actor] if self.auto_approve else [])

    def stop(self, run_id: str) -> Run:
        """Safe-stop: in-flight stages finish, everything not started is skipped."""
        live = self._live.get(run_id)
        if live is not None:  # the running loop will notice and wind down
            if not live.stop_requested:
                live.stop_requested = True
                self._record(live, "stop", "stop requested; finishing in-flight stages, skipping the rest", "requested")
            self._wake[run_id].set()
            self._save(live)
            return live
        run = self._require(run_id)
        if run.state in TERMINAL_RUN_STATES:
            return run
        run.stop_requested = True
        self._record(run, "stop", "stop requested while no stage was executing", "requested")
        if run.metrics.started_at is None:
            run.metrics.started_at = now()
            self.telemetry.run_started(run.id)
        self._skip_unstarted(run)
        self._settle(run)
        self._save(run)
        return run

    def get_run(self, run_id: str) -> Optional[Run]:
        return self._live.get(run_id) or self.store.load_run(run_id)

    # ================================================================ main loop
    async def _drive(self, run: Run) -> None:
        if run.metrics.started_at is None:
            run.metrics.started_at = now()
            self.telemetry.run_started(run.id)
            self._record(run, "start", "run started", "running")
        else:
            self._record(run, "start", "run resumed", "resumed")
        run.state = RunState.RUNNING
        self._recover_interrupted(run)

        order = topological_order(s.spec for s in run.stages.values())
        wake = self._wake[run.id] = asyncio.Event()
        tasks: dict[asyncio.Task, str] = {}
        try:
            while True:
                if run.stop_requested:
                    self._skip_unstarted(run)
                else:
                    for stage_id in self._advance(run, order):
                        task = asyncio.create_task(self._execute(run, run.stages[stage_id]))
                        tasks[task] = stage_id
                self._save(run)
                if not tasks:
                    break
                # Sleep until a stage finishes OR approve()/stop() pokes us. No polling.
                wake_task = asyncio.ensure_future(wake.wait())
                done, _ = await asyncio.wait({*tasks, wake_task}, return_when=asyncio.FIRST_COMPLETED)
                wake_task.cancel()
                wake.clear()
                for task in sorted(done - {wake_task}, key=lambda t: order.index(tasks[t])):
                    self._on_stage_done(run, tasks.pop(task), task)
        finally:
            for task in tasks:  # only non-empty if the engine itself crashed
                task.cancel()
        self._settle(run)
        self._save(run)

    def _advance(self, run: Run, order: list[str]) -> list[str]:
        """One scheduling pass. Skips dead branches, evaluates entry gates and
        approvals, and returns the ids of stages to launch right now."""
        launches: list[str] = []
        for stage_id in order:  # topological, so a skip cascades down in one pass
            st = run.stages[stage_id]
            if st.state in (StageState.PENDING, StageState.READY):  # READY = a human approved it meanwhile
                approved = st.state == StageState.READY
                status = dependency_status(st.spec, {i: s.state for i, s in run.stages.items()})
                if status == DEAD:
                    st.state = StageState.SKIPPED
                    self._record(run, "stage", "an upstream stage failed or was skipped", "skipped", stage_id,
                                 depends_on=st.spec.depends_on)
                elif status == READY and self._admit(run, st, approved):
                    self._begin(run, st)
                    launches.append(stage_id)
                elif status != READY and approved:  # the world changed while it waited: start over, ask again
                    st.state = StageState.PENDING
                    self._record(run, "stage", "dependencies changed after approval; waiting again", "requeued",
                                 stage_id)
        return launches

    def _admit(self, run: Run, st: StageRuntime, approved: bool = False) -> bool:
        """Entry gates, then approval. True means: launch now. ``approved``: a human already said yes, but
        evidence may have changed during the wait, so the entry gates are checked again."""
        results = self._evaluate_gates(run, st, st.spec.entry_gates, "entry")
        failed = [r for r in results if not r.passed]
        if failed:
            st.state = StageState.BLOCKED
            st.error = "; ".join(f"{r.gate}: {r.message}" for r in failed)
            st.ended_at = now()
            self._record(run, "stage", "entry gate failed" + (" after approval" if approved else "") + ", stage blocked",
                         "blocked", st.spec.id, error=st.error, after_approval=approved)
            return False
        if approved:
            return True
        if self.policy.requires_human(st.spec):
            return self._request_approval(run, st)
        return True

    def _on_stage_done(self, run: Run, stage_id: str, task: asyncio.Task) -> None:
        st = run.stages[stage_id]
        try:
            changed = task.result()
        except BaseException as exc:  # a bug in the engine itself; still must not crash the run
            st.state, st.error, st.ended_at = StageState.FAILED, f"internal engine error: {exc!r}", now()
            self._record(run, "stage", st.error, "failed", stage_id)
            return
        if st.state == StageState.SUCCEEDED:
            if self._replan_if_self_stale(run, st):
                return  # it will run again on fresh input; its outputs are republished then
            if changed:
                self._maybe_replan(run, stage_id, changed)

    # ============================================================ stage execution
    def _begin(self, run: Run, st: StageRuntime) -> None:
        """Mark a stage RUNNING and remember exactly which input versions it saw."""
        spec = st.spec
        st.state = StageState.RUNNING
        st.started_at, st.ended_at = now(), None
        st.attempts, st.error, st.used_fallback = 0, None, False
        st.runs += 1
        st.input_hashes = {n: run.artifacts[n].sha256 if n in run.artifacts else "" for n in spec.consumes}
        self._record(run, "stage", f"stage started (execution #{st.runs})", "started", spec.id,
                     input_hashes=st.input_hashes)

    async def _execute(self, run: Run, st: StageRuntime) -> set[str]:
        """Run one stage to a terminal state. Returns the artifact names whose
        content changed (used for re-planning). Never raises for agent errors."""
        spec = st.spec
        try:
            changed = await self._attempts(run, st, spec.agent, spec.max_attempts, fallback=False)
            if changed is None and spec.fallback_agent:
                self._check_not_stopped(run)
                run.metrics.fallbacks += 1
                st.used_fallback = True
                self._record(run, "fallback", f"retries exhausted; trying fallback agent '{spec.fallback_agent}'",
                             "attempted", spec.id, error=st.error)
                changed = await self._attempts(run, st, spec.fallback_agent, 1, fallback=True)
            if changed is not None:
                st.state, st.error = StageState.SUCCEEDED, None
                self._record(run, "stage", "stage succeeded", "succeeded", spec.id, attempts=st.attempts,
                             used_fallback=st.used_fallback)
                return changed
            await self._fail(run, st)
        except _StopRequested:
            # stop() arrived between attempts: not a failure of the stage, just work we chose not to start.
            st.state = StageState.SKIPPED
            self._record(run, "stop", f"stop requested; remaining attempts skipped (last error: {st.error})",
                         "skipped", spec.id)
        except PolicyViolation as violation:
            # Hard stop: no retry, no fallback, no rollback agent. Safe-stop the rest.
            st.state, st.error = StageState.FAILED, str(violation)
            self._record(run, "policy", violation.message, "fail", spec.id, rule=violation.rule)
            if not run.stop_requested:
                run.stop_requested = True
                self._record(run, "stop", "policy violation triggered an automatic safe-stop", "requested",
                             spec.id, rule=violation.rule)
        finally:
            st.ended_at = now()
        return set()

    async def _attempts(self, run: Run, st: StageRuntime, agent_name: str, max_attempts: int,
                        fallback: bool) -> Optional[set[str]]:
        """Try ``agent_name`` up to ``max_attempts`` times with exponential
        backoff. Returns changed artifact names on success, None if all failed."""
        spec = st.spec
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self._check_not_stopped(run)  # the in-flight attempt was allowed to end; no new ones
            st.attempts += 1
            error, changed = await self._attempt(run, st, agent_name, fallback)
            if error is None:
                return changed
            st.error = error
            self._record(run, "retry", f"attempt {attempt}/{max_attempts} with '{agent_name}' failed: {error}",
                         "failed", spec.id, agent=agent_name, fallback=fallback)
            if attempt < max_attempts:
                run.metrics.retries += 1
                delay_ms = spec.backoff_ms * 2 ** (attempt - 1)
                self._record(run, "retry", f"retrying in {delay_ms} ms", "scheduled", spec.id, delay_ms=delay_ms)
                await self._sleep(delay_ms / 1000)
        return None

    @staticmethod
    def _check_not_stopped(run: Run) -> None:
        if run.stop_requested:
            raise _StopRequested()

    async def _attempt(self, run: Run, st: StageRuntime, agent_name: str,
                       fallback: bool) -> tuple[Optional[str], set[str]]:
        """One agent call + policy + exit gates. Returns (error, changed).
        Artifacts are only published if the whole attempt succeeds."""
        ctx = self._context(run, st, fallback)
        timeout_ms = st.spec.timeout_ms
        try:
            call = asyncio.get_running_loop().run_in_executor(self._pool, self.agents[agent_name], ctx)
            # Known limitation: on timeout we stop WAITING, but a Python thread cannot be force-killed, so the
            # agent keeps running in its worker thread until it returns (its late result is discarded).
            result = await (asyncio.wait_for(call, timeout_ms / 1000) if timeout_ms else call)
        except asyncio.TimeoutError:
            return f"timeout after {timeout_ms} ms", set()
        except Exception as exc:  # agent crashed: that is a failed attempt, not an engine crash
            return str(exc) or type(exc).__name__, set()
        if not isinstance(result, AgentResult):
            return f"agent '{agent_name}' returned {type(result).__name__}, expected AgentResult", set()

        for name, content in result.artifacts.items():
            self.policy.check_artifact(name, content)  # PolicyViolation propagates: hard stop

        gate_results = self._evaluate_gates(run, st, st.spec.exit_gates, "exit", overlay=result.artifacts)
        failed = [r for r in gate_results if not r.passed]
        if failed:
            return "exit gate failed: " + "; ".join(f"{r.gate}: {r.message}" for r in failed), set()
        return None, self._publish(run, st, result.artifacts)

    def _publish(self, run: Run, st: StageRuntime, produced: dict[str, dict[str, Any]]) -> set[str]:
        changed: set[str] = set()
        for name, content in produced.items():
            previous = run.artifacts.get(name)
            digest = fingerprint(content)
            run.artifacts[name] = Artifact(
                name=name, content=copy.deepcopy(content), sha256=digest, produced_by=st.spec.id,
                version=previous.version + 1 if previous else 1,
            )
            if previous is None or previous.sha256 != digest:
                changed.add(name)
            self._record(run, "artifact", f"artifact '{name}' published", "published", st.spec.id,
                         artifact=name, sha256=digest, version=run.artifacts[name].version)
        return changed

    async def _fail(self, run: Run, st: StageRuntime) -> None:
        """Retries and fallback are spent: roll back if we can, else FAILED."""
        spec = st.spec
        failure = st.error
        if not spec.rollback_agent:
            st.state = StageState.FAILED
            self._record(run, "stage", f"stage failed: {failure}", "failed", spec.id)
            return
        if run.stop_requested:  # no new agent launches once stop is requested, rollback included
            st.state = StageState.FAILED
            st.error = f"{failure}; rollback agent not launched: stop requested"
            self._record(run, "rollback", "stop requested; rollback agent not launched", "skipped", spec.id)
            return
        run.metrics.rollbacks += 1
        ctx = self._context(run, st, False, rollback=True, failure=failure)
        try:
            await asyncio.get_running_loop().run_in_executor(self._pool, self.agents[spec.rollback_agent], ctx)
        except Exception as exc:
            st.state = StageState.FAILED
            st.error = f"{failure}; rollback also failed: {exc}"
            self._record(run, "rollback", f"rollback agent failed: {exc}", "fail", spec.id)
            return
        st.state = StageState.ROLLED_BACK
        self._record(run, "rollback", f"stage failed ({failure}); rollback agent '{spec.rollback_agent}' ran",
                     "applied", spec.id)

    # ================================================================ approvals
    def _request_approval(self, run: Run, st: StageRuntime) -> bool:
        """Create the Approval. Returns True if auto-approved (launch now),
        False if the stage must wait for a human."""
        spec = st.spec
        n = sum(1 for a in run.approvals.values() if a.stage_id == spec.id) + 1
        kind = approval_kind_of(spec) or "generic"
        approval = Approval(id=f"{spec.id}-{n}", run_id=run.id, stage_id=spec.id, kind=kind)
        run.approvals[approval.id] = approval
        run.metrics.approvals_requested += 1
        self._record(run, "approval", f"approval '{approval.kind}' requested for stage '{spec.id}'", "requested",
                     spec.id, approval_id=approval.id, approval_kind=approval.kind)
        if self.auto_approve is not None and self.auto_approve.allows(kind):
            # Still a full audit trail: a real Approval + decision, just with a machine actor.
            actor = self.auto_approve.actor
            approval.status, approval.actor = ApprovalStatus.APPROVED, actor
            approval.resolved_at, approval.comment = now(), "granted by the engine's AutoApprovePolicy"
            self._record(run, "approval", f"auto-approved by the engine's AutoApprovePolicy ({actor})", "approved",
                         spec.id, approval_id=approval.id, actor=actor, high_impact=kind in HIGH_IMPACT)
            return True
        st.state = StageState.WAITING_APPROVAL
        return False

    async def _resolve(self, run_id: str, approval_id: str, actor: str, comment: str,
                       status: ApprovalStatus) -> Run:
        live = self._live.get(run_id)
        if live is not None:  # other branches are still executing: just record and wake the loop
            self._apply_resolution(live, approval_id, actor, comment, status)
            self._wake[run_id].set()
            self._save(live)
            return live
        run = self._require(run_id)  # paused (or after a restart): record, persist, resume
        self._apply_resolution(run, approval_id, actor, comment, status)
        self._save(run)
        return await self.run(run_id)

    def _apply_resolution(self, run: Run, approval_id: str, actor: str, comment: str,
                          status: ApprovalStatus) -> None:
        approval = run.approvals.get(approval_id)
        if approval is None:
            raise ValueError(f"unknown approval '{approval_id}' for run '{run.id}'")
        if approval.status != ApprovalStatus.PENDING:
            raise ValueError(f"approval '{approval_id}' is already {approval.status.value}")
        st = run.stages[approval.stage_id]
        approval.status, approval.actor = status, actor
        approval.resolved_at, approval.comment = now(), comment
        if status == ApprovalStatus.APPROVED:
            st.state = StageState.READY  # the loop launches it
        else:
            # Nothing ran yet, so there is nothing to roll back: the stage simply
            # fails and the normal skip-downstream handling takes over.
            st.state, st.ended_at = StageState.FAILED, now()
            st.error = f"approval rejected by {actor}" + (f": {comment}" if comment else "")
        self._record(run, "approval", comment or f"{status.value} by {actor}", status.value, st.spec.id,
                     approval_id=approval_id, actor=actor, approval_kind=approval.kind)

    def _cancel_approvals(self, run: Run, stage_id: str, reason: str) -> None:
        for approval in run.approvals.values():
            if approval.stage_id == stage_id and approval.status == ApprovalStatus.PENDING:
                approval.status, approval.actor = ApprovalStatus.REJECTED, "engine"
                approval.resolved_at, approval.comment = now(), reason

    # ================================================================ re-plan
    def _maybe_replan(self, run: Run, source_id: str, changed: set[str]) -> None:
        """``source_id`` just published ``changed`` artifacts. Any already
        SUCCEEDED stage that consumed an older version (or ran before the
        artifact existed) worked from stale input: reset it, and everything
        downstream of it, to PENDING so the main loop runs it again.

        Stages that are RUNNING right now are left alone here; ``_replan_if_self_stale`` catches them
        when they finish."""
        if run.stop_requested:
            return
        stale: dict[str, dict[str, dict[str, str]]] = {}
        for stage_id, st in run.stages.items():
            if stage_id != source_id and st.state == StageState.SUCCEEDED:
                diff = self._stale_inputs(run, st, changed)
                if diff:
                    stale[stage_id] = diff
        self._replan(run, source_id, stale, f"'{source_id}' changed {sorted(changed)}", exclude={source_id})

    def _replan_if_self_stale(self, run: Run, st: StageRuntime) -> bool:
        """A stage just succeeded. If an input it consumed changed while it was RUNNING, its result is built on
        stale data: re-plan it (and everything downstream). Returns True if it was reset or failed."""
        if run.stop_requested:
            return False
        own = {n for n in st.spec.consumes if n in run.artifacts and run.artifacts[n].produced_by != st.spec.id}
        diff = self._stale_inputs(run, st, own)
        return bool(diff) and self._replan(run, st.spec.id, {st.spec.id: diff},
                                           f"'{st.spec.id}' finished on inputs that changed while it ran")

    @staticmethod
    def _stale_inputs(run: Run, st: StageRuntime, names: set[str]) -> dict[str, dict[str, str]]:
        """artifact -> {consumed, current} for the ``names`` this stage consumed in an older version."""
        return {
            name: {"consumed": st.input_hashes.get(name) or "(absent)", "current": run.artifacts[name].sha256}
            for name in sorted(names)
            if name in st.spec.consumes and name in run.artifacts and st.input_hashes.get(name) != run.artifacts[name].sha256
        }

    def _replan(self, run: Run, source_id: str, stale: dict[str, dict[str, dict[str, str]]], reason: str,
                exclude: frozenset[str] | set[str] = frozenset()) -> bool:
        """Reset the stale stages and everything downstream of them. If ``source_id`` already caused
        MAX_REPLANS re-plans, FAIL the run's stale stages instead (never silently carry on with stale work)."""
        if not stale:
            return False
        specs = [s.spec for s in run.stages.values()]
        to_reset = set(stale)
        for stage_id in stale:
            to_reset |= downstream(specs, stage_id)
        to_reset = {i for i in to_reset
                    if i not in exclude and run.stages[i].state not in (StageState.PENDING, StageState.RUNNING)}
        if not to_reset:
            return False

        reset_ids = [i for i in run.stages if i in to_reset]  # graph order, for a readable record
        applied = sum(1 for d in run.decisions if d.kind == "replan" and d.outcome == "applied" and d.stage_id == source_id)
        if applied >= MAX_REPLANS:
            failed = [i for i in reset_ids if run.stages[i].state == StageState.SUCCEEDED]
            for stage_id in failed:
                st = run.stages[stage_id]
                st.state, st.error, st.ended_at = StageState.FAILED, "stale_after_replan_limit", now()
            self._record(run, "replan", f"{reason}, but '{source_id}' already caused {MAX_REPLANS} re-plans: "
                         f"failing stale stages {failed} instead of continuing with stale results",
                         "limit_exceeded", source_id, stale=stale, failed=failed)
            return True
        for stage_id in reset_ids:
            self._reset_stage(run, run.stages[stage_id])
        run.metrics.replans += 1
        why = "; ".join(f"'{s}' consumed outdated {sorted(d)}" for s, d in stale.items())
        self._record(run, "replan", f"{reason}: {why}. Re-running {reset_ids}",
                     "applied", source_id, stale=stale, reset=reset_ids, changed=sorted({n for d in stale.values() for n in d}))
        return True

    def _reset_stage(self, run: Run, st: StageRuntime) -> None:
        self._cancel_approvals(run, st.spec.id, "superseded by re-plan")
        st.state = StageState.PENDING
        st.attempts, st.error, st.used_fallback = 0, None, False
        st.started_at = st.ended_at = None
        st.input_hashes = {}  # `runs` is kept: it counts executions across re-plans

    # ================================================================ finishing
    def _skip_unstarted(self, run: Run) -> None:
        for stage_id, st in run.stages.items():
            if st.state in (StageState.PENDING, StageState.READY, StageState.WAITING_APPROVAL):
                self._cancel_approvals(run, stage_id, "run stopped")
                st.state = StageState.SKIPPED
                self._record(run, "stop", "run is stopping; stage never started", "skipped", stage_id)

    def _settle(self, run: Run) -> None:
        """Nothing is executing. Decide: paused for approval, or finished?"""
        stages = list(run.stages.values())
        if any(s.state == StageState.WAITING_APPROVAL for s in stages):
            run.state = RunState.WAITING_APPROVAL
            self._record(run, "approval", "run paused until pending approvals are resolved", "paused")
            return
        for st in stages:  # defensive: unreachable for a valid graph, but never leave a run half-open
            if st.state not in TERMINAL_STAGE_STATES:
                st.state = StageState.SKIPPED
                self._record(run, "stage", "no progress possible; stage skipped", "skipped", st.spec.id)
        states = {s.state for s in stages}
        if states & {StageState.FAILED, StageState.BLOCKED}:
            run.state = RunState.FAILED
        elif StageState.ROLLED_BACK in states:
            run.state = RunState.ROLLED_BACK
        elif states == {StageState.SUCCEEDED}:
            run.state = RunState.SUCCEEDED
        elif run.stop_requested:
            run.state = RunState.STOPPED
        else:
            run.state = RunState.FAILED
        self._finish(run)

    def _finish(self, run: Run) -> None:
        m = run.metrics
        if m.ended_at is not None:  # run_finished must fire exactly once
            return
        m.ended_at = now()
        m.e2e_latency_ms = (m.ended_at - (m.started_at or m.ended_at)) * 1000
        m.approval_wait_ms = self._approval_wait_ms(run, m.ended_at)
        m.active_ms = max(0.0, m.e2e_latency_ms - m.approval_wait_ms)  # what the system itself spent
        states = [s.state for s in run.stages.values()]
        m.stages_succeeded = states.count(StageState.SUCCEEDED)
        m.stages_failed = sum(s in (StageState.FAILED, StageState.ROLLED_BACK, StageState.BLOCKED) for s in states)
        self._record(run, "finish", f"run finished as {run.state.value}", run.state.value,
                     succeeded=m.stages_succeeded, failed=m.stages_failed)
        self.telemetry.run_finished(run)

    @staticmethod
    def _approval_wait_ms(run: Run, ended_at: float) -> float:
        """Wall time during which at least one approval was pending (overlapping waits count once)."""
        spans = sorted((a.requested_at, a.resolved_at if a.resolved_at is not None else ended_at)
                       for a in run.approvals.values())
        total, cursor = 0.0, None
        for start, end in spans:
            if cursor is not None and start < cursor:
                start = cursor
            if end > start:
                total += end - start
            cursor = end if cursor is None else max(cursor, end)
        return total * 1000

    def _recover_interrupted(self, run: Run) -> None:
        """After a crash/restart a stage may be stuck in RUNNING with no task
        behind it. Agents are re-runnable, so put it back to PENDING.

        Known limitation: if that stage needed approval, the earlier (granted) approval is not reused, so
        it asks for a fresh one."""
        for stage_id, st in run.stages.items():
            if st.state == StageState.RUNNING:
                st.state = StageState.PENDING
                self._record(run, "stage", "stage was interrupted by a restart; re-queued", "recovered", stage_id)

    # ================================================================ small helpers
    def _evaluate_gates(self, run: Run, st: StageRuntime, names: list[str], phase: str,
                        overlay: Optional[dict[str, dict[str, Any]]] = None) -> list[GateResult]:
        if not names:
            return []
        artifacts = {n: copy.deepcopy(a.content) for n, a in run.artifacts.items()}
        artifacts.update(copy.deepcopy(overlay or {}))
        ctx = GateContext(
            run_id=run.id, stage_id=st.spec.id, scenario=run.scenario, artifacts=artifacts,
            stage_states={i: s.state.value for i, s in run.stages.items()},
            params=copy.deepcopy(run.params),
            approvals=[a.model_dump(mode="json") for a in run.approvals.values()],
        )
        results = run_gates(names, ctx, self.gates)
        for r in results:
            self._record(run, "gate", r.message, "pass" if r.passed else "fail", st.spec.id,
                         gate=r.gate, phase=phase, details=r.details)
        return results

    def _context(self, run: Run, st: StageRuntime, fallback: bool, **extra_params: Any) -> AgentContext:
        """Read-only snapshot for the agent (deep-copied: it runs in another thread)."""
        return AgentContext(
            run_id=run.id, stage_id=st.spec.id, scenario=run.scenario, requirement=run.requirement,
            attempt=st.attempts, is_fallback=fallback,
            artifacts={n: copy.deepcopy(a.content) for n, a in run.artifacts.items()},
            params={**copy.deepcopy(run.params), **extra_params},
        )

    def _record(self, run: Run, kind: str, rationale: str, outcome: str,
                stage_id: Optional[str] = None, **inputs: Any) -> None:
        """Every significant event: one DecisionRecord on the run + one telemetry event."""
        run.decisions.append(DecisionRecord(run_id=run.id, stage_id=stage_id, kind=kind,
                                            rationale=rationale, outcome=outcome, inputs=inputs))
        self.telemetry.emit(run.id, kind, stage_id, outcome=outcome, rationale=rationale, **inputs)

    def _save(self, run: Run) -> None:
        self.store.save_run(run)

    def _require(self, run_id: str) -> Run:
        run = self.store.load_run(run_id)
        if run is None:
            raise KeyError(f"unknown run '{run_id}'")
        return run
