"""Engine tests. Agents are plain functions; sleep is a recording no-op so
backoff never slows the suite; a JSON-round-tripping store proves that a Run is
fully serialisable (and lets us 'restart' by building a second Engine)."""
import asyncio
import threading

import pytest

from orchestrator.engine import Engine
from orchestrator.models import (
    AgentResult, ApprovalStatus, Autonomy, Run, RunState as R, StageSpec, StageState as S,
)
from orchestrator.policy import PolicyViolation

WAIT_S = 5  # upper bound for any cross-thread wait, so a bug fails fast instead of hanging


# ------------------------------------------------------------------ fakes
class JsonStore:
    """Stores runs as JSON strings: what you get back is a fresh copy."""

    def __init__(self):
        self.raw: dict[str, str] = {}

    def save_run(self, run):
        self.raw[run.id] = run.model_dump_json()

    def load_run(self, run_id):
        raw = self.raw.get(run_id)
        return Run.model_validate_json(raw) if raw else None

    def list_runs(self):
        return [self.load_run(i) for i in self.raw]


class FakeTelemetry:
    def __init__(self):
        self.events, self.started, self.finished = [], [], []

    def emit(self, run_id, event, stage_id=None, **details):
        self.events.append((event, stage_id, details))

    def run_started(self, run_id):
        self.started.append(run_id)

    def run_finished(self, run):
        self.finished.append(run.id)

    def events_for(self, run_id):
        return []

    def snapshot(self):
        return {}


class Sleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, seconds):
        self.delays.append(seconds)


def spec(id, deps=(), agent=None, **kw):
    kw.setdefault("backoff_ms", 10)
    return StageSpec(id=id, title=id, agent=agent or id, depends_on=list(deps), **kw)


def make(name="out", **content):
    """Agent that publishes one artifact."""
    return lambda ctx: AgentResult(artifacts={name: content or {"ok": True}})


class Calls:
    """Wraps an agent function and counts/logs its invocations (thread-safe enough: list.append)."""

    def __init__(self, fn):
        self.fn, self.log = fn, []

    def __call__(self, ctx):
        self.log.append(ctx)
        return self.fn(ctx)

    @property
    def count(self):
        return len(self.log)


@pytest.fixture
def sleeper():
    return Sleeper()


@pytest.fixture
def build(sleeper):
    engines = []

    def _build(agents, **kw):
        kw.setdefault("sleep", sleeper)
        engine = Engine(agents, **kw)
        engines.append(engine)
        return engine

    yield _build
    for e in engines:
        e.close()


async def execute(engine, graph, **params):
    run = engine.create_run("test", "build a thing", graph, params)
    return await engine.run(run.id)


def decisions(run, kind, outcome=None):
    return [d for d in run.decisions if d.kind == kind and (outcome is None or d.outcome == outcome)]


# ------------------------------------------------------------ happy paths
async def test_linear_run_succeeds_and_publishes_artifacts(build):
    seen = {}

    def b(ctx):
        seen["a_visible_to_b"] = ctx.artifacts.get("a_out")
        return AgentResult(artifacts={"b_out": {"n": 2}})

    engine = build({"a": make("a_out", n=1), "b": b, "c": make("c_out")})
    run = await execute(engine, [spec("a"), spec("b", ["a"], consumes=["a_out"]), spec("c", ["b"])])

    assert run.state == R.SUCCEEDED
    assert {s.state for s in run.stages.values()} == {S.SUCCEEDED}
    assert seen["a_visible_to_b"] == {"n": 1}
    art = run.artifacts["a_out"]
    assert art.produced_by == "a" and art.version == 1 and len(art.sha256) == 16
    assert run.stages["b"].input_hashes == {"a_out": art.sha256}  # recorded at start
    assert run.stages["b"].runs == 1
    m = run.metrics
    assert (m.stages_succeeded, m.stages_failed, m.retries, m.replans) == (3, 0, 0, 0)
    assert m.started_at and m.ended_at and m.e2e_latency_ms >= 0
    assert decisions(run, "finish")[0].outcome == "succeeded"


async def test_terminal_run_is_returned_unchanged_and_concurrent_run_rejected(build):
    gate = threading.Event()

    def slow(ctx):
        gate.wait(WAIT_S)
        return AgentResult()

    engine = build({"a": slow})
    run = engine.create_run("t", "r", [spec("a")])
    task = asyncio.create_task(engine.run(run.id))
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeError, match="already executing"):
        await engine.run(run.id)
    gate.set()
    done = await task
    assert done.state == R.SUCCEEDED
    again = await engine.run(run.id)
    assert again.metrics.ended_at == done.metrics.ended_at


async def test_whole_run_is_json_serialisable(build):
    engine = build({"a": make()})
    run = await execute(engine, [spec("a")])
    assert Run.model_validate_json(run.model_dump_json()) == run


# ----------------------------------------------------------- parallelism
async def test_parallel_branches_overlap_and_join_waits_for_all(build):
    barrier = threading.Barrier(2, timeout=WAIT_S)  # both agents must be inside at once
    order, lock = [], threading.Lock()

    def branch(name):
        def fn(ctx):
            barrier.wait()  # raises BrokenBarrierError if the branches were serial
            with lock:
                order.append(name)
            return AgentResult(artifacts={name: {"x": 1}})
        return fn

    def join(ctx):
        with lock:
            order.append("join")
        assert {"b", "c"} <= set(ctx.artifacts)
        return AgentResult()

    engine = build({"a": make("a"), "b": branch("b"), "c": branch("c"), "d": join})
    run = await execute(engine, [spec("a"), spec("b", ["a"]), spec("c", ["a"]), spec("d", ["b", "c"])])

    assert run.state == R.SUCCEEDED
    assert order[-1] == "join" and set(order[:2]) == {"b", "c"}
    assert run.stages["b"].started_at <= run.stages["c"].ended_at  # intervals intersect (barrier is the real proof)
    assert run.stages["c"].started_at <= run.stages["b"].ended_at


async def test_join_any_starts_as_soon_as_one_dependency_succeeds(build):
    release_slow, order = threading.Event(), []

    def slow(ctx):
        assert release_slow.wait(WAIT_S), "join=any stage never started while 'slow' was still running"
        order.append("slow_done")
        return AgentResult()

    def after(ctx):
        order.append("after")
        release_slow.set()
        return AgentResult()

    engine = build({"fast": make(), "slow": slow, "after": after})
    graph = [spec("fast"), spec("slow"), spec("after", ["fast", "slow"], join="any")]
    run = await execute(engine, graph)
    assert run.state == R.SUCCEEDED
    assert order == ["after", "slow_done"]


async def test_join_any_skipped_only_when_all_dependencies_dead(build):
    def boom(ctx):
        raise RuntimeError("x")

    engine = build({"a": boom, "b": boom, "c": make()})
    run = await execute(engine, [spec("a", max_attempts=1), spec("b", max_attempts=1),
                                 spec("c", ["a", "b"], join="any")])
    assert run.stages["c"].state == S.SKIPPED and run.state == R.FAILED


# --------------------------------------------- retries / fallback / rollback
async def test_retry_with_exponential_backoff_then_success(build, sleeper):
    attempts = Calls(lambda ctx: (_ for _ in ()).throw(RuntimeError("flaky")) if ctx.attempt < 3
                     else AgentResult(artifacts={"out": {"ok": 1}}))
    engine = build({"a": attempts})
    run = await execute(engine, [spec("a", max_attempts=3, backoff_ms=100)])

    assert run.state == R.SUCCEEDED
    assert attempts.count == 3 and run.stages["a"].attempts == 3
    assert run.metrics.retries == 2
    assert sleeper.delays == [0.1, 0.2]  # backoff_ms * 2**(attempt-1)
    assert run.stages["a"].error is None
    assert len(decisions(run, "retry", "failed")) == 2


async def test_retries_exhausted_then_fallback_succeeds(build):
    primary = Calls(lambda ctx: (_ for _ in ()).throw(RuntimeError("primary down")))
    fallback = Calls(lambda ctx: AgentResult(artifacts={"out": {"via": "fallback"}}) if ctx.is_fallback
                     else (_ for _ in ()).throw(AssertionError("is_fallback not set")))
    engine = build({"a": primary, "a_fb": fallback, "b": make("b_out")})
    run = await execute(engine, [spec("a", max_attempts=2, fallback_agent="a_fb"), spec("b", ["a"])])

    assert run.state == R.SUCCEEDED
    assert primary.count == 2 and fallback.count == 1
    st = run.stages["a"]
    assert st.used_fallback and st.state == S.SUCCEEDED
    assert (run.metrics.retries, run.metrics.fallbacks, run.metrics.rollbacks) == (1, 1, 0)
    assert run.artifacts["out"].content == {"via": "fallback"}
    assert decisions(run, "fallback")


async def test_fallback_not_used_when_primary_succeeds(build):
    fb = Calls(make())
    engine = build({"a": make(), "a_fb": fb})
    run = await execute(engine, [spec("a", fallback_agent="a_fb")])
    assert fb.count == 0 and not run.stages["a"].used_fallback


async def test_fallback_fails_then_rollback_and_downstream_skipped(build):
    def boom(ctx):
        raise RuntimeError("always broken")

    rollback = Calls(lambda ctx: AgentResult())
    engine = build({"a": make(), "b": boom, "b_fb": boom, "b_undo": rollback, "c": make(), "d": make()})
    graph = [spec("a"), spec("b", ["a"], max_attempts=2, fallback_agent="b_fb", rollback_agent="b_undo"),
             spec("c", ["b"]), spec("d", ["c"])]
    run = await execute(engine, graph)

    assert run.state == R.ROLLED_BACK
    assert run.stages["b"].state == S.ROLLED_BACK
    assert run.stages["c"].state == S.SKIPPED and run.stages["d"].state == S.SKIPPED
    assert rollback.count == 1
    assert rollback.log[0].params["rollback"] is True and "always broken" in rollback.log[0].params["failure"]
    assert (run.metrics.retries, run.metrics.fallbacks, run.metrics.rollbacks) == (1, 1, 1)
    assert run.metrics.stages_succeeded == 1 and run.metrics.stages_failed == 1
    assert decisions(run, "rollback", "applied")


async def test_failure_without_rollback_marks_failed_and_captures_error(build):
    def boom(ctx):
        raise ValueError("bad input 42")

    engine = build({"a": boom, "b": make()})
    run = await execute(engine, [spec("a", max_attempts=1), spec("b", ["a"])])
    assert run.state == R.FAILED
    assert run.stages["a"].state == S.FAILED and run.stages["a"].error == "bad input 42"
    assert run.stages["b"].state == S.SKIPPED


async def test_failing_rollback_leaves_stage_failed(build):
    def boom(ctx):
        raise RuntimeError("main")

    def bad_undo(ctx):
        raise RuntimeError("undo broke")

    engine = build({"a": boom, "a_undo": bad_undo})
    run = await execute(engine, [spec("a", max_attempts=1, rollback_agent="a_undo")])
    assert run.state == R.FAILED and "undo broke" in run.stages["a"].error


async def test_failure_in_one_branch_does_not_stop_independent_branch(build):
    def boom(ctx):
        raise RuntimeError("x")

    engine = build({"bad": boom, "good": make(), "after_good": make(), "after_bad": make()})
    run = await execute(engine, [spec("bad", max_attempts=1), spec("good"),
                                 spec("after_good", ["good"]), spec("after_bad", ["bad"])])
    assert run.stages["after_good"].state == S.SUCCEEDED
    assert run.stages["after_bad"].state == S.SKIPPED
    assert run.state == R.FAILED


# ------------------------------------------------------------------ gates
async def test_exit_gate_failure_counts_as_failed_attempt_and_retries(build):
    def tester(ctx):
        failed = 1 if ctx.attempt == 1 else 0
        return AgentResult(artifacts={"test_report": {"passed": 4, "failed": failed}})

    calls = Calls(tester)
    engine = build({"t": calls})
    run = await execute(engine, [spec("t", exit_gates=["tests_green"], max_attempts=2)])

    assert run.state == R.SUCCEEDED and calls.count == 2
    assert run.metrics.retries == 1
    assert run.artifacts["test_report"].content["failed"] == 0
    assert run.artifacts["test_report"].version == 1  # the rejected attempt was never published
    assert [d.outcome for d in decisions(run, "gate")] == ["fail", "pass"]


async def test_exit_gate_failing_every_time_fails_stage(build):
    engine = build({"t": make("test_report", passed=1, failed=3)})
    run = await execute(engine, [spec("t", exit_gates=["tests_green"], max_attempts=2)])
    assert run.state == R.FAILED
    assert "tests_green" in run.stages["t"].error
    assert "test_report" not in run.artifacts


async def test_entry_gate_failure_blocks_stage_and_skips_downstream(build):
    ran = Calls(make())
    engine = build({"a": make(), "b": ran, "c": make()})
    run = await execute(engine, [spec("a"), spec("b", ["a"], entry_gates=["architecture_present"]),
                                 spec("c", ["b"])])
    assert run.stages["b"].state == S.BLOCKED and ran.count == 0
    assert "architecture" in run.stages["b"].error
    assert run.stages["c"].state == S.SKIPPED
    assert run.state == R.FAILED
    assert decisions(run, "gate", "fail")


async def test_entry_gate_passes_when_evidence_exists(build):
    engine = build({"a": make("architecture", components=["api"]), "b": make()})
    run = await execute(engine, [spec("a"), spec("b", ["a"], entry_gates=["architecture_present"])])
    assert run.state == R.SUCCEEDED


async def test_unknown_gate_name_fails_closed(build):
    engine = build({"a": make()})
    run = await execute(engine, [spec("a", entry_gates=["typo_gate"])])
    assert run.stages["a"].state == S.BLOCKED


async def test_custom_gate_registry(build):
    from orchestrator.models import GateResult
    gates = {"always_no": lambda c: GateResult(gate="always_no", passed=False, message="no")}
    engine = build({"a": make()}, gates=gates)
    run = await execute(engine, [spec("a", entry_gates=["always_no"])])
    assert run.stages["a"].state == S.BLOCKED


# -------------------------------------------------------------- approvals
def approval_graph():
    return [spec("a"), spec("ship", ["a"], requires_approval=True, approval_kind="public_api_change"),
            spec("after", ["ship"])]


async def test_approval_pauses_run_and_resumes_after_restart_with_new_engine(build):
    store = JsonStore()
    agents1 = {"a": Calls(make()), "ship": Calls(make()), "after": Calls(make())}
    engine1 = build(agents1, store=store)
    run = await execute(engine1, approval_graph())

    # Paused: run() returned, nothing blocks, state is durable.
    assert run.state == R.WAITING_APPROVAL
    assert run.stages["a"].state == S.SUCCEEDED
    assert run.stages["ship"].state == S.WAITING_APPROVAL
    assert run.stages["after"].state == S.PENDING
    assert agents1["ship"].count == 0
    [approval] = run.approvals.values()
    assert approval.kind == "public_api_change" and approval.status == ApprovalStatus.PENDING
    assert run.metrics.approvals_requested == 1 and run.metrics.ended_at is None

    # "Restart": a brand-new engine that only shares the store.
    agents2 = {"a": Calls(make()), "ship": Calls(make()), "after": Calls(make())}
    engine2 = build(agents2, store=store)
    resumed = await engine2.approve(run.id, approval.id, actor="alice", comment="looks fine")

    assert resumed.state == R.SUCCEEDED
    assert agents2["a"].count == 0  # finished work is not redone
    assert agents2["ship"].count == 1 and agents2["after"].count == 1
    done = resumed.approvals[approval.id]
    assert done.status == ApprovalStatus.APPROVED and done.actor == "alice" and done.comment == "looks fine"
    assert done.resolved_at is not None
    d = decisions(resumed, "approval", "approved")[0]
    assert d.inputs["actor"] == "alice"
    assert store.load_run(run.id).state == R.SUCCEEDED


async def test_human_gated_autonomy_requires_approval(build):
    engine = build({"a": make()})
    run = await execute(engine, [spec("a", autonomy=Autonomy.HUMAN_GATED)])
    assert run.state == R.WAITING_APPROVAL
    assert next(iter(run.approvals.values())).kind == "generic"


async def test_reject_fails_stage_skips_downstream_and_ends_failed(build):
    store = JsonStore()
    engine = build({"a": make(), "ship": Calls(make()), "after": make()}, store=store)
    run = await execute(engine, approval_graph())
    [approval] = run.approvals.values()
    done = await engine.reject(run.id, approval.id, actor="bob", comment="not ready")

    assert done.state == R.FAILED
    assert done.stages["ship"].state == S.FAILED and "bob" in done.stages["ship"].error
    assert done.stages["after"].state == S.SKIPPED
    assert done.approvals[approval.id].status == ApprovalStatus.REJECTED
    assert done.approvals[approval.id].actor == "bob"
    assert decisions(done, "approval", "rejected")
    assert done.metrics.ended_at is not None


async def test_resolving_an_approval_twice_or_unknown_is_an_error(build):
    engine = build({"a": make(), "ship": make(), "after": make()})
    run = await execute(engine, approval_graph())
    [approval] = run.approvals.values()
    await engine.approve(run.id, approval.id, "alice")
    with pytest.raises(ValueError, match="already approved"):
        await engine.approve(run.id, approval.id, "alice")
    with pytest.raises(ValueError, match="unknown approval"):
        await engine.approve(run.id, "nope", "alice")


async def test_auto_approve_runs_straight_through_but_keeps_audit_trail(build):
    engine = build({"a": make(), "ship": make(), "after": make()})
    run = await execute(engine, approval_graph(), auto_approve=True)

    assert run.state == R.SUCCEEDED
    [approval] = run.approvals.values()
    assert approval.status == ApprovalStatus.APPROVED and approval.actor == "auto-approver"
    assert approval.resolved_at is not None
    outcomes = [d.outcome for d in decisions(run, "approval")]
    assert outcomes == ["requested", "approved"]
    assert decisions(run, "approval", "approved")[0].inputs["actor"] == "auto-approver"
    assert run.metrics.approvals_requested == 1


async def test_independent_branches_keep_running_while_approval_pending(build):
    engine = build({"gated": make(), "side": make("side_out"), "side2": make()})
    graph = [spec("gated", requires_approval=True), spec("side"), spec("side2", ["side"]),
             spec("after_gated", ["gated"], agent="side2")]
    run = await execute(engine, graph)
    assert run.state == R.WAITING_APPROVAL
    assert run.stages["side"].state == S.SUCCEEDED and run.stages["side2"].state == S.SUCCEEDED
    assert run.stages["gated"].state == S.WAITING_APPROVAL
    assert run.stages["after_gated"].state == S.PENDING


async def test_approve_while_other_branch_is_still_running_wakes_the_loop(build):
    release = threading.Event()

    def slow(ctx):
        assert release.wait(WAIT_S)
        return AgentResult(artifacts={"slow_out": {"x": 1}})

    gated = Calls(make("gated_out"))
    engine = build({"slow": slow, "gated": gated})
    run = engine.create_run("t", "r", [spec("slow"), spec("gated", requires_approval=True)])
    task = asyncio.create_task(engine.run(run.id))

    for _ in range(200):  # wait (event loop stays free) until the approval request exists
        await asyncio.sleep(0.01)
        live = engine.get_run(run.id)
        if live.approvals:
            break
    approval_id = next(iter(live.approvals))
    returned = await engine.approve(run.id, approval_id, "carol")
    assert returned.state == R.RUNNING  # live path: does not block on the whole run
    for _ in range(200):  # the approved stage runs even though 'slow' is still busy
        await asyncio.sleep(0.01)
        if gated.count:
            break
    assert gated.count == 1 and not release.is_set()
    release.set()
    final = await asyncio.wait_for(task, WAIT_S)
    assert final.state == R.SUCCEEDED


# -------------------------------------------------------------------- stop
async def test_stop_lets_inflight_stage_finish_and_skips_the_rest(build):
    started, release = threading.Event(), threading.Event()

    def a(ctx):
        started.set()
        assert release.wait(WAIT_S)
        return AgentResult(artifacts={"a_out": {"x": 1}})

    later = Calls(make())
    engine = build({"a": a, "b": later, "c": later})
    run = engine.create_run("t", "r", [spec("a"), spec("b", ["a"]), spec("c", ["b"])])
    task = asyncio.create_task(engine.run(run.id))
    while not started.is_set():
        await asyncio.sleep(0.01)

    stopped_view = engine.stop(run.id)
    assert stopped_view.stop_requested
    release.set()
    final = await asyncio.wait_for(task, WAIT_S)

    assert final.state == R.STOPPED
    assert final.stages["a"].state == S.SUCCEEDED  # in-flight stage was allowed to finish
    assert final.stages["b"].state == S.SKIPPED and final.stages["c"].state == S.SKIPPED
    assert later.count == 0
    assert "a_out" in final.artifacts
    assert decisions(final, "stop")


async def test_stop_a_paused_run_cancels_pending_approval(build):
    store, telemetry = JsonStore(), FakeTelemetry()
    engine = build({"a": make(), "ship": make(), "after": make()}, store=store, telemetry=telemetry)
    run = await execute(engine, approval_graph())
    assert run.state == R.WAITING_APPROVAL

    stopped = engine.stop(run.id)
    assert stopped.state == R.STOPPED
    assert stopped.stages["ship"].state == S.SKIPPED and stopped.stages["after"].state == S.SKIPPED
    assert next(iter(stopped.approvals.values())).status == ApprovalStatus.REJECTED
    assert telemetry.finished == [run.id]
    assert store.load_run(run.id).state == R.STOPPED
    assert engine.stop(run.id).state == R.STOPPED  # idempotent
    assert telemetry.finished == [run.id]  # still exactly once


# ------------------------------------------------------------------ policy
async def test_secret_in_artifact_is_a_hard_stop_with_no_retry(build):
    leaky = Calls(lambda ctx: AgentResult(artifacts={"implementation": {"config": 'password = "hunter2xyz"'}}))
    fb, undo, nxt = Calls(make()), Calls(make()), Calls(make())
    engine = build({"a": leaky, "fb": fb, "undo": undo, "next": nxt, "side": make()})
    graph = [spec("a", max_attempts=3, fallback_agent="fb", rollback_agent="undo"),
             spec("next", ["a"]), spec("side")]
    run = await execute(engine, graph)

    assert leaky.count == 1  # not retried
    assert fb.count == 0 and undo.count == 0  # no fallback, no rollback
    assert run.stages["a"].state == S.FAILED and "secret_detected" in run.stages["a"].error
    assert nxt.count == 0 and run.stages["next"].state == S.SKIPPED
    assert run.state == R.FAILED and run.stop_requested
    assert run.metrics.retries == 0 and run.metrics.fallbacks == 0
    assert "implementation" not in run.artifacts  # nothing leaked into the run
    [violation] = decisions(run, "policy")
    assert violation.outcome == "fail" and violation.inputs["rule"] == "secret_detected"
    assert "hunter2xyz" not in run.model_dump_json()


async def test_policy_violation_safe_stops_other_branches(build):
    started, release = threading.Event(), threading.Event()

    def long_branch(ctx):
        started.set()
        assert release.wait(WAIT_S)
        return AgentResult()

    def leaky(ctx):
        assert started.wait(WAIT_S)
        return AgentResult(artifacts={"api": {"destination": "javascript:alert(1)"}})

    never = Calls(make())
    engine = build({"long": long_branch, "leaky": leaky, "never": never})
    run = engine.create_run("t", "r", [spec("long"), spec("leaky"), spec("never", ["long"])])
    task = asyncio.create_task(engine.run(run.id))
    while not run_has_policy_decision(engine, run.id):
        await asyncio.sleep(0.01)
    release.set()
    final = await asyncio.wait_for(task, WAIT_S)
    assert final.stages["long"].state == S.SUCCEEDED  # in-flight work completes
    assert final.stages["never"].state == S.SKIPPED and never.count == 0
    assert final.state == R.FAILED


def run_has_policy_decision(engine, run_id):
    run = engine.get_run(run_id)
    return any(d.kind == "policy" for d in run.decisions)


def test_create_run_rejects_policy_violating_stage(build):
    engine = build({"release": make()})
    with pytest.raises(PolicyViolation):
        engine.create_run("t", "r", [spec("release", autonomy=Autonomy.FULL)])
    with pytest.raises(PolicyViolation):
        engine.create_run("t", "r", [spec("x", agent="release", approval_kind="schema_change")])


# --------------------------------------------------------------- re-plan
async def test_replan_resets_downstream_when_upstream_artifact_changes(build):
    """'consumer' (and its dependent 'report') finish BEFORE 'producer' publishes
    the 'spec' artifact they declared they consume. When it lands they used stale
    input, so both are reset and run again, this time seeing the spec."""
    report_done = threading.Event()
    seen_specs = []

    def producer(ctx):
        assert report_done.wait(WAIT_S)
        return AgentResult(artifacts={"spec": {"v": "final"}})

    def consumer(ctx):
        seen_specs.append(ctx.artifacts.get("spec"))
        return AgentResult(artifacts={"analysis": {"spec_seen": ctx.artifacts.get("spec", {}).get("v")}})

    def report(ctx):
        report_done.set()
        return AgentResult(artifacts={"report": {"analysis": ctx.artifacts["analysis"]}})

    calls = {k: Calls(v) for k, v in {"producer": producer, "consumer": consumer, "report": report}.items()}
    engine = build(calls)
    run = await execute(engine, [spec("producer"), spec("consumer", consumes=["spec"]),
                                 spec("report", ["consumer"], consumes=["analysis"])])

    assert run.state == R.SUCCEEDED
    assert (calls["producer"].count, calls["consumer"].count, calls["report"].count) == (1, 2, 2)
    assert seen_specs == [None, {"v": "final"}]  # second pass saw the new artifact
    assert run.stages["consumer"].runs == 2 and run.stages["report"].runs == 2
    assert run.stages["producer"].runs == 1
    assert run.metrics.replans == 1
    assert run.artifacts["analysis"].content == {"spec_seen": "final"}
    assert run.artifacts["analysis"].version == 2
    assert run.stages["consumer"].input_hashes["spec"] == run.artifacts["spec"].sha256

    [replan] = decisions(run, "replan")
    assert replan.outcome == "applied" and replan.stage_id == "producer"
    assert replan.inputs["reset"] == ["consumer", "report"]
    assert replan.inputs["changed"] == ["spec"]
    assert "consumer" in replan.rationale and "spec" in replan.rationale


async def test_no_replan_when_consumer_already_saw_current_artifact(build):
    engine = build({"a": make("spec", v=1), "b": make("out")})
    run = await execute(engine, [spec("a"), spec("b", ["a"], consumes=["spec"])])
    assert run.metrics.replans == 0 and run.stages["b"].runs == 1


async def test_replan_is_capped_by_circuit_breaker(build, monkeypatch):
    import orchestrator.engine as eng
    monkeypatch.setattr(eng, "MAX_REPLANS", 0)
    done = threading.Event()

    def producer(ctx):
        assert done.wait(WAIT_S)
        return AgentResult(artifacts={"spec": {"v": 1}})

    def consumer(ctx):
        done.set()
        return AgentResult()

    engine = build({"producer": producer, "consumer": consumer})
    run = await execute(engine, [spec("producer"), spec("consumer", consumes=["spec"])])
    assert run.state == R.SUCCEEDED and run.metrics.replans == 0
    assert decisions(run, "replan", "suppressed")
    assert run.stages["consumer"].runs == 1


# ---------------------------------------------------- validation / telemetry
@pytest.mark.parametrize("graph, message", [
    ([spec("a", ["b"]), spec("b", ["a"])], "cycle"),
    ([spec("a", ["ghost"])], "unknown"),
    ([spec("a"), spec("a")], "duplicate"),
    ([spec("a", agent="missing_agent")], "unregistered agent"),
])
def test_invalid_graph_rejected_at_create_run(build, graph, message):
    engine = build({"a": make(), "b": make()})
    with pytest.raises(ValueError, match=message):
        engine.create_run("t", "r", graph)


async def test_telemetry_sees_start_once_finish_once_and_events(build):
    telemetry = FakeTelemetry()
    engine = build({"a": make(), "b": make()}, telemetry=telemetry)
    run = engine.create_run("t", "r", [spec("a"), spec("b", ["a"])])
    await engine.run(run.id)
    assert telemetry.started == [run.id] and telemetry.finished == [run.id]
    kinds = {e[0] for e in telemetry.events}
    assert {"start", "stage", "artifact", "finish"} <= kinds
    assert len(telemetry.events) == len(run.decisions)  # every decision is also emitted


async def test_telemetry_start_and_finish_once_across_a_pause(build):
    telemetry, store = FakeTelemetry(), JsonStore()
    agents = {"a": make(), "ship": make(), "after": make()}
    engine = build(agents, store=store, telemetry=telemetry)
    run = await execute(engine, approval_graph())
    assert telemetry.started == [run.id] and telemetry.finished == []
    [approval] = run.approvals.values()
    await build(agents, store=store, telemetry=telemetry).approve(run.id, approval.id, "alice")
    assert telemetry.started == [run.id] and telemetry.finished == [run.id]


async def test_metrics_aggregate_across_a_messy_run(build):
    def flaky(ctx):
        if ctx.attempt < 2:
            raise RuntimeError("once")
        return AgentResult()

    def boom(ctx):
        raise RuntimeError("always")

    engine = build({"ok": make(), "flaky": flaky, "boom": boom, "fb": make(), "undo": make(), "x": make()})
    graph = [spec("ok"), spec("flaky", max_attempts=2), spec("boom", max_attempts=2, fallback_agent="boom",
                                                              rollback_agent="undo"),
             spec("x", ["boom"])]
    run = await execute(engine, graph)
    m = run.metrics
    assert run.state == R.ROLLED_BACK
    assert (m.retries, m.fallbacks, m.rollbacks) == (2, 1, 1)
    assert (m.stages_succeeded, m.stages_failed) == (2, 1)
    assert m.e2e_latency_ms == pytest.approx((m.ended_at - m.started_at) * 1000)


async def test_agent_returning_garbage_is_a_failed_attempt_not_a_crash(build):
    engine = build({"a": lambda ctx: {"not": "an AgentResult"}})
    run = await execute(engine, [spec("a", max_attempts=1)])
    assert run.state == R.FAILED and "AgentResult" in run.stages["a"].error


async def test_agent_cannot_mutate_the_run_through_its_context(build):
    def sneaky(ctx):
        ctx.artifacts["a_out"]["x"] = "tampered"
        return AgentResult()

    engine = build({"a": make("a_out", x="original"), "b": sneaky})
    run = await execute(engine, [spec("a"), spec("b", ["a"])])
    assert run.artifacts["a_out"].content == {"x": "original"}


async def test_interrupted_running_stage_is_requeued_on_resume(build):
    store = JsonStore()
    agents = {"a": Calls(make())}
    engine = build(agents, store=store)
    run = engine.create_run("t", "r", [spec("a")])
    crashed = store.load_run(run.id)
    crashed.stages["a"].state = S.RUNNING  # simulate a process killed mid-stage
    crashed.state = R.RUNNING
    crashed.metrics.started_at = 1.0
    store.save_run(crashed)
    final = await build(agents, store=store).run(run.id)
    assert final.state == R.SUCCEEDED and agents["a"].count == 1
    assert decisions(final, "stage", "recovered")
