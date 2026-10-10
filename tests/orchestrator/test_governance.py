"""Governance hardening of the engine: what a human is asked about, what is stored about it, and what may enter.

* the initial requirement is bounded and policy-scanned like an amended one (before any agent can read it);
* comments of humans are policy-scanned before they are stored in the approval, decision and audit trail;
* an amendment keeps the old requirement text, and an approval request records the hash of the evidence;
* a stage that reads the requirement TEXT (``reads_requirement``) is re-run by an amendment, even if every
  artifact it consumes is unchanged, and also when the amendment lands while it is running.
"""
import asyncio
import json
import threading

import pytest

from orchestrator.engine import MAX_REQUIREMENT_CHARS
from orchestrator.models import AgentResult, Autonomy, RunState as R
from orchestrator.policy import PolicyViolation
from orchestrator.telemetry import Telemetry

from .test_amend import NEW, OLD, agents, graph, make_engine, paused_run, pending_ids  # noqa: F401  (fixture)
from .test_engine import Calls, decisions, spec

SECRET = "hunter2hunter2"
WAIT_S = 5


# ------------------------------------------------- the initial requirement
def test_create_run_refuses_a_requirement_with_a_secret_before_any_agent_runs(make_engine):
    fns = agents()
    engine = make_engine(fns)
    with pytest.raises(PolicyViolation) as refused:
        engine.create_run("test", f"add aliases, password={SECRET}", graph())
    assert refused.value.rule == "secret_detected" and SECRET not in str(refused.value)
    assert engine.store.list_runs() == [] and all(f.count == 0 for f in fns.values())


def test_create_run_refuses_an_unsafe_url_and_an_oversized_requirement(make_engine):
    engine = make_engine(agents())
    with pytest.raises(PolicyViolation):
        engine.create_run("test", 'see <a href="javascript:alert(1)">this</a>', graph())
    with pytest.raises(ValueError, match="at most 2000"):
        engine.create_run("test", "x" * (MAX_REQUIREMENT_CHARS + 1), graph())
    assert engine.create_run("test", "x" * MAX_REQUIREMENT_CHARS, graph()).requirement == "x" * MAX_REQUIREMENT_CHARS


# --------------------------------------------------------------- comments
async def test_an_amend_comment_with_a_secret_is_refused_and_audited(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    with pytest.raises(PolicyViolation):
        await engine.amend_requirement(run.id, NEW, "alice", f"ticket 7, api_key={SECRET}")
    run = engine.get_run(run.id)
    assert run.requirement == OLD and pending_ids(run) == ["gate-1"]            # nothing changed
    assert not decisions(run, "replan", "requirement_amended")
    [refusal] = decisions(run, "policy", "fail")
    assert refusal.inputs["actor"] == "alice" and "comment" in refusal.rationale
    assert SECRET not in json.dumps([d.model_dump() for d in run.decisions])   # the secret is not stored anywhere


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_an_approval_comment_with_a_secret_is_refused_and_the_approval_stays_pending(make_engine, verb):
    engine = make_engine(agents())
    run = await paused_run(engine)
    with pytest.raises(PolicyViolation):
        await getattr(engine, verb)(run.id, "gate-1", "bob", f"looks fine, password={SECRET}")
    run = engine.get_run(run.id)
    assert pending_ids(run) == ["gate-1"] and run.approvals["gate-1"].comment == ""
    assert len(decisions(run, "policy", "fail")) == 1
    assert SECRET not in json.dumps([d.model_dump() for d in run.decisions])
    with pytest.raises(ValueError, match="at most 2000"):
        await getattr(engine, verb)(run.id, "gate-1", "bob", "x" * 2001)
    run = await getattr(engine, verb)(run.id, "gate-1", "bob", "reviewed the evidence")  # an ordinary comment is fine
    assert run.approvals["gate-1"].comment == "reviewed the evidence"


# ------------------------------------------------------------ audit trail
async def test_an_amendment_keeps_the_old_requirement_text_in_the_audit_trail(make_engine, tmp_path):
    audit = tmp_path / "audit.jsonl"
    telemetry = Telemetry(audit_path=str(audit))
    engine = make_engine(agents(), telemetry=telemetry)
    run = await paused_run(engine)
    run = await engine.amend_requirement(run.id, NEW, "alice", "expiry too")
    telemetry.close()

    [amend] = decisions(run, "replan", "requirement_amended")
    assert amend.inputs["old_requirement"] == OLD and amend.inputs["new_requirement"] == NEW
    events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    [event] = [e for e in events if e["event"] == "replan" and e["details"]["outcome"] == "requirement_amended"]
    assert event["details"]["old_requirement"] == OLD and event["details"]["new_requirement"] == NEW


async def test_an_amendment_bounds_the_stored_old_text(make_engine):
    engine = make_engine(agents())
    run = engine.create_run("test", OLD, graph())
    run = await engine.run(run.id)
    run.requirement = "y" * (MAX_REQUIREMENT_CHARS + 500)  # e.g. a run stored before the cap existed
    engine.store.save_run(run)
    run = await engine.amend_requirement(run.id, NEW, "alice")
    [amend] = decisions(run, "replan", "requirement_amended")
    assert len(amend.inputs["old_requirement"]) == MAX_REQUIREMENT_CHARS


async def test_an_approval_request_records_the_hash_of_the_evidence_under_review(make_engine):
    engine = make_engine(agents())
    run = await paused_run(engine)
    [requested] = decisions(run, "approval", "requested")
    evidence = requested.inputs["evidence_hashes"]
    assert set(evidence) == {"a_out", "b_out"}                       # exactly what the gate stage consumes
    assert evidence == {n: run.artifacts[n].sha256 for n in ("a_out", "b_out")}

    run = await engine.amend_requirement(run.id, NEW, "alice")       # the evidence changes: a NEW request, new hash
    first, second = decisions(run, "approval", "requested")
    assert first.inputs["evidence_hashes"]["a_out"] != second.inputs["evidence_hashes"]["a_out"]
    assert second.inputs["evidence_hashes"]["a_out"] == run.artifacts["a_out"].sha256


# ------------------------------------------- stages that read the requirement text
def doc_graph(reads_requirement):
    """req publishes an artifact that ignores the requirement (what an LLM may do: same 'problem' for any wording);
    doc consumes only that artifact but quotes the requirement text, like the real documentation agent."""
    return [spec("req"), spec("doc", ["req"], consumes=["spec"], reads_requirement=reads_requirement),
            spec("gate", ["doc"], requires_approval=True, approval_kind="public_api_change",
                 autonomy=Autonomy.HUMAN_GATED, consumes=["doc_out"])]


def doc_agents(**overrides):
    fns = {"req": lambda ctx: AgentResult(artifacts={"spec": {"problem": "the same for every wording"}}),
           "doc": lambda ctx: AgentResult(artifacts={"doc_out": {"changelog": f"Request: {ctx.requirement}"}}),
           "gate": lambda ctx: AgentResult(artifacts={"gated": {"ok": True}})}
    fns.update(overrides)
    return {name: fn if isinstance(fn, Calls) else Calls(fn) for name, fn in fns.items()}


async def test_an_amendment_reruns_a_stage_that_reads_the_requirement_text_even_if_its_artifacts_are_unchanged(make_engine):
    for reads, expected_runs, expected_text in [(True, 2, NEW), (False, 1, OLD)]:
        engine = make_engine(doc_agents())
        run = await engine.run(engine.create_run("test", OLD, doc_graph(reads)).id)
        assert pending_ids(run) == ["gate-1"]
        run = await engine.amend_requirement(run.id, NEW, "alice")
        assert run.artifacts["spec"].version == 2 and run.stages["req"].runs == 2
        assert run.stages["doc"].runs == expected_runs        # False: the stale-changelog defect this field fixes
        assert run.artifacts["doc_out"].content["changelog"] == f"Request: {expected_text}"


async def test_a_requirement_reading_stage_is_run_again_when_the_amendment_lands_while_it_runs(make_engine):
    started, release = threading.Event(), threading.Event()
    seen = []

    def doc(ctx):
        seen.append(ctx.requirement)
        if len(seen) == 1:
            started.set()
            assert release.wait(WAIT_S)
        return AgentResult(artifacts={"doc_out": {"changelog": f"Request: {ctx.requirement}"}})

    engine = make_engine(doc_agents(doc=doc))
    run = engine.create_run("test", OLD, doc_graph(True))
    task = asyncio.create_task(engine.run(run.id))
    while not started.is_set():
        await asyncio.sleep(0.01)
    await engine.amend_requirement(run.id, NEW, "alice")
    release.set()
    final = await asyncio.wait_for(task, WAIT_S)

    assert seen == [OLD, NEW]
    assert final.artifacts["doc_out"].content == {"changelog": f"Request: {NEW}"}
    assert final.state == R.WAITING_APPROVAL and pending_ids(final) == ["gate-1"]
