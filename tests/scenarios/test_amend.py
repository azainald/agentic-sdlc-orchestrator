"""End to end: amend the requirement of a real brownfield run (real agents, real hashes, in-memory store).

Original request: 'add custom aliases'. Amended request: 'add custom aliases and expiring links'. The new feature
changes requirements, impact and the architecture; the verification of the ALREADY existing code (impl_alias,
impl_ratelimit) reproduces the same evidence, and the test-suite stage consumes only the merged implementation,
which does not change, so it keeps its result."""
import pytest

from orchestrator.models import ApprovalStatus, RunState as R, StageState as S
from orchestrator.store import InMemoryRunStore
from scenarios.registry import get_scenario

from .helpers import make_engine, pending

OLD = "Extend the URL shortener: add custom aliases"
NEW = "Extend the URL shortener: add custom aliases and expiring links"


def brownfield(engine, requirement=OLD):
    scenario = get_scenario("brownfield", llm=False)
    return engine.create_run("brownfield", requirement, scenario.graph, {"skip_pytest": True})


def runs(run):
    return {i: s.runs for i, s in run.stages.items()}


def decisions(run, kind, outcome=None):
    return [d for d in run.decisions if d.kind == kind and (outcome is None or d.outcome == outcome)]


async def test_amend_at_the_api_review_asks_for_the_approval_again_on_the_new_evidence():
    engine = make_engine()
    run = await engine.run(brownfield(engine).id)
    assert run.state == R.WAITING_APPROVAL and [a.id for a in pending(run)] == ["api_change_review-1"]
    old_changes = [c["feature"] for c in run.artifacts["impact"].content["public_api_changes"]]
    assert old_changes == ["alias"]
    assert all(run.stages[i].runs == 0 for i in ("impl_alias", "impl_ttl", "impl_ratelimit"))  # not started yet

    run = await engine.amend_requirement(run.id, NEW, "alice", "product wants expiry too")

    # requirements, impact and architecture were re-derived from the new text
    assert run.requirement == NEW
    assert [run.stages[i].runs for i in ("requirements", "impact", "architecture")] == [2, 2, 2]
    new_changes = [c["feature"] for c in run.artifacts["impact"].content["public_api_changes"]]
    assert new_changes == ["alias", "ttl"] and "ttl" in run.artifacts["architecture"].content["features"]

    # the approval granted nothing yet, but the pending one was about the OLD impact: it is withdrawn
    first = run.approvals["api_change_review-1"]
    assert first.status == ApprovalStatus.REJECTED and first.actor == "engine"
    assert [a.id for a in pending(run)] == ["api_change_review-2"] and run.state == R.WAITING_APPROVAL
    assert run.stages["api_change_review"].state == S.WAITING_APPROVAL
    assert all(run.stages[i].runs == 0 for i in ("impl_alias", "impl_ttl", "impl_ratelimit"))  # still behind the gate
    assert decisions(run, "approval", "cancelled")[0].inputs["approval_ids"] == ["api_change_review-1"]
    assert "ttl" in " ".join(c["change"] for c in run.artifacts["impact"].content["public_api_changes"])

    # the human decides again, this time looking at the new evidence
    with pytest.raises(ValueError, match="already rejected"):
        await engine.approve(run.id, "api_change_review-1", "bob")
    run = await engine.approve(run.id, "api_change_review-2", "bob")
    assert run.state == R.WAITING_APPROVAL and [a.id for a in pending(run)] == ["release-1"]
    assert all(run.stages[i].runs == 1 for i in ("impl_alias", "impl_ttl", "impl_ratelimit", "sync", "testing"))
    run = await engine.approve(run.id, "release-1", "carol")
    assert run.state == R.SUCCEEDED


async def test_amend_at_the_release_gate_reruns_only_what_the_new_requirement_touches():
    engine = make_engine()
    run = await engine.run(brownfield(engine).id)
    run = await engine.approve(run.id, "api_change_review-1", "alice")
    assert run.state == R.WAITING_APPROVAL and [a.id for a in pending(run)] == ["release-1"]
    assert set(runs(run).values()) == {1, 0} and runs(run)["release"] == 0
    before = {n: (a.version, a.sha256) for n, a in run.artifacts.items()}

    run = await engine.amend_requirement(run.id, NEW, "alice", "product wants expiry too")

    # nothing but the human gates can be waiting, and BOTH gates need a fresh decision
    assert run.state == R.WAITING_APPROVAL and [a.id for a in pending(run)] == ["api_change_review-2"]
    assert run.approvals["release-1"].status == ApprovalStatus.REJECTED  # withdrawn: its evidence is obsolete
    assert run.approvals["api_change_review-1"].status == ApprovalStatus.APPROVED  # history stays as it was
    run = await engine.approve(run.id, "api_change_review-2", "bob")
    assert run.state == R.WAITING_APPROVAL and [a.id for a in pending(run)] == ["release-2"]

    # executions per stage: derived stages ran again, the unaffected test-suite stage kept its result
    assert runs(run) == {"requirements": 2, "impact": 2, "architecture": 2, "api_change_review": 2,
                         "impl_alias": 2, "impl_ttl": 2, "impl_ratelimit": 2, "sync": 2,
                         "testing": 1, "documentation": 2, "release": 0}

    def changed(name):
        return run.artifacts[name].sha256 != before[name][1]

    # why: only these artifacts really changed ...
    assert {n for n in run.artifacts if n in before and changed(n)} == {
        "requirements", "impact", "architecture", "api_change_review", "impl_ttl", "documentation"}
    # ... the verification of alias and rate limiting reproduced the same evidence (re-ran, same hash) ...
    assert not changed("impl_alias") and not changed("impl_ratelimit") and not changed("implementation")
    assert run.artifacts["impl_alias"].version == 2 and run.artifacts["impl_ttl"].content["covered_by_architecture"]
    # ... so the merged implementation did not change and the test report was never re-produced
    assert run.artifacts["test_report"].version == 1 and run.artifacts["implementation"].version == 2
    assert run.artifacts["documentation"].version == 2
    assert "expiring links" in run.artifacts["requirements"].content["problem"]

    # governance lineage: one amendment decision, the hash-comparison re-plans it caused, and the cancelled approval
    [amended] = decisions(run, "replan", "requirement_amended")
    assert amended.inputs["actor"] == "alice" and amended.inputs["cancelled_approvals"] == ["release-1"]
    applied = [(d.stage_id, d.inputs["reset"]) for d in decisions(run, "replan", "applied")]
    assert applied[0][0] == "requirements" and "impact" in applied[0][1] and "documentation" in applied[0][1]
    assert not any("testing" in reset for _, reset in applied)
    assert run.metrics.replans == 1 + len(applied)

    # and the release still needs a human, on the new evidence
    run = await engine.approve(run.id, "release-2", "carol")
    assert run.state == R.SUCCEEDED and run.metrics.approvals_requested == 4
    assert run.stages["release"].runs == 1


async def test_amend_survives_a_restart_and_the_amended_run_finishes():
    store = InMemoryRunStore()
    first = make_engine(store=store)
    run = await first.run(brownfield(first).id)
    first.close()

    second = make_engine(store=store)  # a different process, same database
    run = await second.amend_requirement(run.id, NEW, "alice")
    assert [a.id for a in pending(run)] == ["api_change_review-2"]
    third = make_engine(store=store)
    run = await third.approve(run.id, "api_change_review-2", "bob")
    run = await third.approve(run.id, "release-1", "carol")
    assert run.state == R.SUCCEEDED and run.requirement == NEW
    assert store.load_run(run.id).requirement == NEW


async def test_a_wording_only_amendment_rederives_the_text_but_keeps_every_approved_decision():
    """Same features, different words: requirements/impact/architecture/documentation consume the new text and run
    again, but impact and architecture reproduce identical artifacts, so the approved API review, the
    implementation checks, sync and the test-suite are all kept. Only the release decision (it rests on the
    regenerated documentation) must be taken again."""
    engine = make_engine()
    run = await engine.run(brownfield(engine).id)
    run = await engine.approve(run.id, "api_change_review-1", "alice")
    assert [a.id for a in pending(run)] == ["release-1"]

    run = await engine.amend_requirement(run.id, OLD + " (please keep the wording short)", "alice")

    assert runs(run) == {"requirements": 2, "impact": 2, "architecture": 2, "api_change_review": 1,
                         "impl_alias": 1, "impl_ttl": 1, "impl_ratelimit": 1, "sync": 1, "testing": 1,
                         "documentation": 2, "release": 0}
    assert run.approvals["api_change_review-1"].status == ApprovalStatus.APPROVED  # still valid: same evidence
    assert run.approvals["release-1"].status == ApprovalStatus.REJECTED and [a.id for a in pending(run)] == ["release-2"]
    run = await engine.approve(run.id, "release-2", "carol")
    assert run.state == R.SUCCEEDED


async def test_amending_to_a_policy_violating_text_is_refused_and_the_run_continues():
    from orchestrator.policy import PolicyViolation
    engine = make_engine()
    run = await engine.run(brownfield(engine).id)
    with pytest.raises(PolicyViolation):
        await engine.amend_requirement(run.id, 'add aliases; password = "hunter2hunter2"', "alice")
    run = engine.get_run(run.id)
    assert run.requirement == OLD and not run.stop_requested and [a.id for a in pending(run)] == ["api_change_review-1"]
    run = await engine.approve(run.id, "api_change_review-1", "bob")
    assert [a.id for a in pending(run)] == ["release-1"]
