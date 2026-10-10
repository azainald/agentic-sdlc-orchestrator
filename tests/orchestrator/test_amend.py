"""Engine tests for Engine.amend_requirement (the human-triggered dynamic re-plan).

The graph is small and every agent counts its executions, so "which stages ran again" is measured, not assumed:

    req ──> a ──> c                req: publishes spec {text: requirement}
     │      │                      a:   consumes spec, publishes a_out (changes with the requirement)
     │      └──> gate ──> final    c:   consumes a_out (changes with the requirement)
     └──> b ──> d                  b:   depends on req but consumes nothing (does not read the requirement)
            └──> gate              d:   consumes b_out (never changes)
                                   gate: human approval, consumes a_out and b_out
"""
import asyncio
import threading

import pytest

from orchestrator.engine import MAX_REQUIREMENT_CHARS, Engine, RunNotAmendable
from orchestrator.models import (
    AgentResult, ApprovalStatus, Autonomy, RunState as R, StageState as S, fingerprint,
)
from orchestrator.policy import PolicyViolation

from .test_engine import Calls, FakeTelemetry, JsonStore, Sleeper, decisions, make, spec

WAIT_S = 5
OLD, NEW = "add custom aliases", "add custom aliases and expiring links"


# ------------------------------------------------------------------ fixtures
def agents(**overrides):
    """Counting agents for the graph in the module docstring."""
    fns = {
        "req": lambda ctx: AgentResult(artifacts={"spec": {"text": ctx.requirement}}),
        "a": lambda ctx: AgentResult(artifacts={"a_out": {"text": ctx.artifacts["spec"]["text"]}}),
        "b": make("b_out", constant=True),
        "c": lambda ctx: AgentResult(artifacts={"c_out": {"size": len(ctx.artifacts["a_out"]["text"])}}),
        "d": make("d_out", constant=True),
        "gate": lambda ctx: AgentResult(artifacts={"approved_text": {"text": ctx.artifacts["a_out"]["text"]}}),
        "final": make("final_out"),
    }
    fns.update(overrides)
    return {name: fn if isinstance(fn, Calls) else Calls(fn) for name, fn in fns.items()}


def graph():
    return [
        spec("req"),
        spec("a", ["req"], consumes=["spec"]),
        spec("b", ["req"]),
        spec("c", ["a"], consumes=["a_out"]),
        spec("d", ["b"], consumes=["b_out"]),
        spec("gate", ["a", "b"], requires_approval=True, approval_kind="public_api_change",
             autonomy=Autonomy.HUMAN_GATED, consumes=["a_out", "b_out"]),
        spec("final", ["gate"]),
    ]


@pytest.fixture
def make_engine():
    engines = []

    def _make(fns, store=None, telemetry=None, **kw):
        engine = Engine(fns, store=store if store is not None else JsonStore(), telemetry=telemetry,
                        sleep=Sleeper(), **kw)
        engines.append(engine)
        return engine

    yield _make
    for e in engines:
        e.close()


async def paused_run(engine, requirement=OLD):
    """Run until the human gate asks for approval."""
    run = engine.create_run("test", requirement, graph())
    run = await engine.run(run.id)
    assert run.state == R.WAITING_APPROVAL and run.stages["gate"].state == S.WAITING_APPROVAL
    return run


def runs(run):
    return {i: s.runs for i, s in run.stages.items()}


def pending_ids(run):
    return [a.id for a in run.approvals.values() if a.status == ApprovalStatus.PENDING]


# ------------------------------------------------------------- happy path
async def test_amend_reruns_only_what_depends_on_the_change_and_asks_for_approval_again(make_engine):
    fns = agents()
    engine = make_engine(fns)
    run = await paused_run(engine)
    assert pending_ids(run) == ["gate-1"]
    assert runs(run) == {"req": 1, "a": 1, "b": 1, "c": 1, "d": 1, "gate": 0, "final": 0}

    run = await engine.amend_requirement(run.id, NEW, "alice", "product wants expiry too")

    # selective: req/a/c depend on the requirement and ran again; b and d do not and kept their results
    assert runs(run) == {"req": 2, "a": 2, "b": 1, "c": 2, "d": 1, "gate": 0, "final": 0}
    assert [fns[n].count for n in ("req", "a", "b", "c", "d")] == [2, 2, 1, 2, 1]
    assert run.requirement == NEW and fns["req"].log[-1].requirement == NEW
    assert run.artifacts["a_out"].content == {"text": NEW} and run.artifacts["a_out"].version == 2
    assert run.artifacts["b_out"].version == 1  # b never ran again

    # governance: the approval given on the old evidence is withdrawn; a new one is asked on the new evidence
    assert run.state == R.WAITING_APPROVAL and run.stages["gate"].state == S.WAITING_APPROVAL
    assert pending_ids(run) == ["gate-2"]
    old = run.approvals["gate-1"]
    assert old.status == ApprovalStatus.REJECTED and old.actor == "engine"
    assert "requirement amended by alice" in old.comment
    [cancelled] = decisions(run, "approval", "cancelled")
    assert cancelled.stage_id == "gate" and cancelled.inputs["approval_ids"] == ["gate-1"]

    # the new evidence is what a human is now asked about: finishing needs a fresh approval
    run = await engine.approve(run.id, "gate-2", "bob")
    assert run.state == R.SUCCEEDED and fns["gate"].log[-1].artifacts["a_out"] == {"text": NEW}
    assert fns["final"].count == 1 and run.stages["gate"].runs == 1


async def test_amendment_is_recorded_as_a_replan_decision_with_lineage(make_engine):
    telemetry = FakeTelemetry()
    engine = make_engine(agents(), telemetry=telemetry)
    run = await paused_run(engine)
    before = run.requirement
    run = await engine.amend_requirement(run.id, f"  {NEW}  ", "  alice ", "because")

    [amend] = decisions(run, "replan", "requirement_amended")
    assert amend.stage_id is None and "requirement amended by alice: because" in amend.rationale
    assert amend.inputs["actor"] == "alice" and amend.inputs["comment"] == "because"
    assert amend.inputs["old_requirement_sha256"] == fingerprint(before)
    assert amend.inputs["new_requirement_sha256"] == fingerprint(NEW)  # stripped
    assert amend.inputs["requeued"] == ["req", "gate"] and amend.inputs["cancelled_approvals"] == ["gate-1"]
    assert "requirement_amended" in [d["outcome"] for event, _, d in telemetry.events if event == "replan"]
    # the hash comparison then explains every follow-up re-run in the same lineage
    applied = decisions(run, "replan", "applied")
    assert [(d.stage_id, d.inputs["reset"]) for d in applied] == [("req", ["a"]), ("a", ["c"])]


async def test_metrics_count_the_amendment_and_each_replan_pass(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    assert run.metrics.replans == 0
    run = await engine.amend_requirement(run.id, NEW, "alice")
    # 1 amendment + req changed spec (a stale) + a changed a_out (c stale)
    assert run.metrics.replans == 3
    run = await engine.approve(run.id, "gate-2", "bob")
    assert run.metrics.replans == 3 and run.metrics.approvals_requested == 2


async def test_amend_when_nothing_downstream_has_run_yet(make_engine):
    fns = agents()
    engine = make_engine(fns)
    run = engine.create_run("test", OLD, graph())  # created, never started
    run = await engine.amend_requirement(run.id, NEW, "alice")
    assert run.state == R.WAITING_APPROVAL and run.requirement == NEW
    assert runs(run)["req"] == 1 and fns["req"].log[0].requirement == NEW and run.metrics.replans == 1


# --------------------------------------------------------------- refusals
@pytest.mark.parametrize("actor", ["", "   ", "auto-approver", "Engine", "system", "demo-auto-approver"])
async def test_amend_refuses_empty_and_reserved_actors(make_engine, actor):
    engine = make_engine(agents())
    run = await paused_run(engine)
    with pytest.raises(ValueError):
        await engine.amend_requirement(run.id, NEW, actor)
    assert engine.get_run(run.id).requirement == OLD and not decisions(engine.get_run(run.id), "replan")


async def test_amend_refuses_the_engines_own_auto_approver_name(make_engine):
    from orchestrator.models import AutoApprovePolicy
    engine = make_engine(agents(), auto_approve=AutoApprovePolicy(kinds=frozenset({"nothing"}), actor="robot-1"))
    run = await paused_run(engine)
    with pytest.raises(ValueError, match="reserved"):
        await engine.amend_requirement(run.id, NEW, "robot-1")


async def test_amend_refuses_a_finished_run(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    run = await engine.approve(run.id, "gate-1", "bob")
    assert run.state == R.SUCCEEDED
    with pytest.raises(RunNotAmendable, match="already succeeded"):
        await engine.amend_requirement(run.id, NEW, "alice")
    assert engine.get_run(run.id).requirement == OLD


async def test_amend_refuses_a_stopping_run(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    engine.stop(run.id)  # a paused run: settles immediately as STOPPED
    with pytest.raises(RunNotAmendable):
        await engine.amend_requirement(run.id, NEW, "alice")


async def test_amend_unknown_run_is_a_key_error(make_engine):
    engine = make_engine(agents())
    with pytest.raises(KeyError):
        await engine.amend_requirement("run-nope", NEW, "alice")


@pytest.mark.parametrize("text, message", [
    ("", "non-empty"),
    ("   \n ", "non-empty"),
    (OLD, "identical"),
    (f"  {OLD}  ", "identical"),
    ("add   custom\taliases", "identical"),  # whitespace-only difference is not a new requirement
    ("x" * (MAX_REQUIREMENT_CHARS + 1), "limit"),
])
async def test_amend_refuses_empty_identical_and_oversized_text(make_engine, text, message):
    engine = make_engine(agents())
    run = await paused_run(engine)
    with pytest.raises(ValueError, match=message):
        await engine.amend_requirement(run.id, text, "alice")
    after = engine.get_run(run.id)
    assert after.requirement == OLD and after.metrics.replans == 0 and pending_ids(after) == ["gate-1"]


async def test_amend_accepts_text_exactly_at_the_limit(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    run = await engine.amend_requirement(run.id, "y" * MAX_REQUIREMENT_CHARS, "alice")
    assert len(run.requirement) == MAX_REQUIREMENT_CHARS


@pytest.mark.parametrize("text, rule", [
    ('add aliases. api_key = "sk-live-1234567890abcdef"', "secret_detected"),
    ('add aliases, see <a href="javascript:alert(1)">docs</a>', "unsafe_url_scheme"),
])
async def test_amend_runs_the_policy_scan_and_changes_nothing_when_it_objects(make_engine, text, rule):
    fns = agents()
    engine = make_engine(fns)
    run = await paused_run(engine)
    with pytest.raises(PolicyViolation) as exc:
        await engine.amend_requirement(run.id, text, "alice")
    assert exc.value.rule == rule and "sk-live" not in str(exc.value)  # names the field, never the value
    after = engine.get_run(run.id)
    # refused, not a hard stop: the run keeps waiting on the SAME approval, only the refusal is audited
    assert after.requirement == OLD and after.state == R.WAITING_APPROVAL and not after.stop_requested
    assert pending_ids(after) == ["gate-1"] and fns["req"].count == 1 and after.metrics.replans == 0
    [policy] = decisions(after, "policy", "fail")
    assert policy.inputs["rule"] == rule and policy.inputs["actor"] == "alice"
    assert "sk-live" not in policy.rationale
    after = await engine.approve(run.id, "gate-1", "bob")  # still fully usable
    assert after.state == R.SUCCEEDED


async def test_amend_refuses_an_overlong_comment(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    with pytest.raises(ValueError, match="comment"):
        await engine.amend_requirement(run.id, NEW, "alice", "c" * 2001)


# ----------------------------------------------------------------- restart
async def test_amend_works_from_a_new_engine_on_the_same_store(make_engine):
    store = JsonStore()
    first = make_engine(agents(), store=store)
    run = await paused_run(first)

    fns = agents()
    second = make_engine(fns, store=store)  # "after a restart": nothing live, only the stored run
    run = await second.amend_requirement(run.id, NEW, "alice")
    assert run.state == R.WAITING_APPROVAL and pending_ids(run) == ["gate-2"]
    assert [fns[n].count for n in ("req", "a", "b", "c", "d")] == [1, 1, 0, 1, 0]  # only the re-run stages executed
    assert store.load_run(run.id).requirement == NEW  # persisted

    third = make_engine(agents(), store=store)  # and the amended, re-approved run can still be finished
    run = await third.approve(run.id, "gate-2", "bob")
    assert run.state == R.SUCCEEDED and run.artifacts["approved_text"].content == {"text": NEW}


async def test_old_approval_cannot_be_used_after_an_amendment(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    await engine.amend_requirement(run.id, NEW, "alice")
    with pytest.raises(ValueError, match="already rejected"):
        await engine.approve(run.id, "gate-1", "bob")


# --------------------------------------------------------------- the cap
async def test_replan_cap_fails_the_run_loudly_when_a_requirement_is_amended_too_often(make_engine, monkeypatch):
    import orchestrator.engine as eng
    monkeypatch.setattr(eng, "MAX_REPLANS", 1)
    engine = make_engine(agents())
    run = await paused_run(engine)
    run = await engine.amend_requirement(run.id, NEW, "alice")  # re-plan #1 from 'req': allowed
    assert run.state == R.WAITING_APPROVAL
    run = await engine.amend_requirement(run.id, NEW + " per tenant", "alice")  # would be #2: the cap trips

    assert run.state == R.FAILED
    [limit] = decisions(run, "replan", "limit_exceeded")
    assert limit.stage_id == "req" and "a" in limit.inputs["failed"]
    assert run.stages["a"].state == S.FAILED and run.stages["a"].error == "stale_after_replan_limit"
    assert run.stages["gate"].state == S.SKIPPED  # nothing is approved on top of stale work
    assert pending_ids(run) == []


# --------------------------------------------------- in-flight (live) runs
async def test_amend_while_the_root_stage_is_running_reruns_it_on_the_new_text(make_engine):
    started, release = threading.Event(), threading.Event()
    seen = []

    def req(ctx):
        seen.append(ctx.requirement)
        if len(seen) == 1:
            started.set()
            assert release.wait(WAIT_S)
        return AgentResult(artifacts={"spec": {"text": ctx.requirement}})

    fns = agents(req=req)
    engine = make_engine(fns)
    run = engine.create_run("test", OLD, graph())
    task = asyncio.create_task(engine.run(run.id))
    while not started.is_set():
        await asyncio.sleep(0.01)

    live = await engine.amend_requirement(run.id, NEW, "alice")  # returns at once: the loop is already running
    assert live.requirement == NEW and live.stages["req"].state == S.RUNNING  # a RUNNING stage is not mutated
    [amend] = decisions(live, "replan", "requirement_amended")
    assert amend.inputs["running_roots"] == ["req"] and amend.inputs["requeued"] == []
    release.set()
    final = await asyncio.wait_for(task, WAIT_S)

    assert seen == [OLD, NEW]
    assert final.stages["req"].runs == 2 and final.artifacts["spec"].content == {"text": NEW}
    assert [fns[n].count for n in ("a", "b", "c", "d")] == [1, 1, 1, 1]  # nothing ran on the stale spec
    assert final.state == R.WAITING_APPROVAL and pending_ids(final) == ["gate-1"]
    assert decisions(final, "replan", "requeued")[0].stage_id == "req"
    final = await engine.approve(final.id, "gate-1", "bob")
    assert final.state == R.SUCCEEDED and final.artifacts["approved_text"].content == {"text": NEW}


async def test_amend_while_a_downstream_stage_is_running_rechecks_it_when_it_finishes(make_engine):
    a_started, a_release = threading.Event(), threading.Event()
    seen = []

    def a(ctx):
        seen.append(ctx.artifacts["spec"]["text"])
        if len(seen) == 1:
            a_started.set()
            assert a_release.wait(WAIT_S)
        return AgentResult(artifacts={"a_out": {"text": ctx.artifacts["spec"]["text"]}})

    fns = agents(a=a)
    engine = make_engine(fns)
    run = engine.create_run("test", OLD, graph())
    task = asyncio.create_task(engine.run(run.id))
    while not a_started.is_set():
        await asyncio.sleep(0.01)

    live = await engine.amend_requirement(run.id, NEW, "alice")
    assert live.stages["a"].state == S.RUNNING  # untouched
    for _ in range(200):  # let the new requirement flow through the loop before 'a' returns
        if fns["req"].count == 2:
            break
        await asyncio.sleep(0.01)
    a_release.set()
    final = await asyncio.wait_for(task, WAIT_S)

    assert seen == [OLD, NEW]  # the first result was built on the old spec, so 'a' ran again
    assert final.artifacts["a_out"].content == {"text": NEW}
    assert fns["c"].log[-1].artifacts["a_out"] == {"text": NEW} and final.stages["c"].state == S.SUCCEEDED
    assert final.state == R.WAITING_APPROVAL and pending_ids(final) == ["gate-1"]
    assert final.artifacts["c_out"].content == {"size": len(NEW)}  # nothing downstream kept stale data


async def test_amend_withdraws_a_pending_approval_while_another_branch_is_still_running(make_engine):
    slow_started, slow_release = threading.Event(), threading.Event()

    def slow(ctx):
        slow_started.set()
        assert slow_release.wait(WAIT_S)
        return AgentResult(artifacts={"slow_out": {"ok": True}})

    fns = agents(slow=slow)
    engine = make_engine(fns)
    g = [*graph(), spec("slow", ["req"])]
    run = engine.create_run("test", OLD, g)
    task = asyncio.create_task(engine.run(run.id))
    while not (slow_started.is_set() and pending_ids(engine.get_run(run.id))):
        await asyncio.sleep(0.01)
    assert engine.get_run(run.id).state == R.RUNNING  # live, gate waiting, slow still busy

    live = await engine.amend_requirement(run.id, NEW, "alice")
    assert live.approvals["gate-1"].status == ApprovalStatus.REJECTED and live.stages["gate"].state == S.PENDING
    with pytest.raises(ValueError, match="already rejected"):
        await engine.approve(run.id, "gate-1", "bob")  # the human's click on stale evidence is refused
    slow_release.set()
    final = await asyncio.wait_for(task, WAIT_S)

    assert final.state == R.WAITING_APPROVAL and pending_ids(final) == ["gate-2"]
    assert final.stages["slow"].runs == 1  # kept: it does not consume anything that changed
    assert fns["gate"].count == 0 and final.artifacts["a_out"].content == {"text": NEW}


async def test_two_concurrent_amendments_are_applied_one_after_the_other(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    results = await asyncio.gather(
        engine.amend_requirement(run.id, NEW, "alice"),
        engine.amend_requirement(run.id, NEW + " per tenant", "bob"),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results)
    final = engine.get_run(run.id)
    assert final.requirement == NEW + " per tenant" and final.state == R.WAITING_APPROVAL
    assert [d.inputs["actor"] for d in decisions(final, "replan", "requirement_amended")] == ["alice", "bob"]
    assert pending_ids(final) and final.artifacts["a_out"].content == {"text": NEW + " per tenant"}
    final = await engine.approve(run.id, pending_ids(final)[0], "carol")
    assert final.state == R.SUCCEEDED
