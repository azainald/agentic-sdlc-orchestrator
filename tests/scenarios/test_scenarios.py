"""The three scenarios end to end through the real Engine (in-memory store, pytest subprocess skipped)."""
import pytest

from orchestrator.gates import GATES
from orchestrator.graph import validate_graph
from orchestrator.models import Autonomy, GateContext, RunState as R, StageState as S
from orchestrator.policy import PolicyEngine
from orchestrator.store import InMemoryRunStore
from orchestrator.telemetry import Telemetry
from scenarios.registry import SCENARIOS, get_scenario, list_scenarios

from .helpers import Sleeper, create, make_engine, pending, start

STAGES = {
    "greenfield": ["requirements", "architecture", "impl_api", "impl_store", "impl_analytics",
                   "sync", "testing", "documentation", "release"],
    "brownfield": ["requirements", "impact", "architecture", "api_change_review", "impl_alias", "impl_ttl",
                   "impl_ratelimit", "sync", "testing", "documentation", "release"],
    "ambiguous": ["requirements", "normalize", "apply_interpretation", "architecture", "impl_ssrf", "impl_audit",
                  "impl_retention", "sync", "testing", "documentation", "release"],
}
IMPLS = {"greenfield": ["impl_api", "impl_store", "impl_analytics"],
         "brownfield": ["impl_alias", "impl_ttl", "impl_ratelimit"],
         "ambiguous": ["impl_ssrf", "impl_audit", "impl_retention"]}


def gate_on(run, name):
    arts = {n: a.content for n, a in run.artifacts.items()}
    return GATES[name](GateContext(run_id=run.id, stage_id="x", scenario=run.scenario, artifacts=arts))


def states(run):
    return {i: s.state for i, s in run.stages.items()}


# ----------------------------------------------------------------------- graphs
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_graph_is_explicit_valid_and_policy_clean(name):
    scenario = get_scenario(name)
    assert [s.id for s in scenario.graph] == STAGES[name]
    validate_graph(scenario.graph)
    policy = PolicyEngine()
    for spec in scenario.graph:
        policy.check_stage(spec)
        assert set(spec.entry_gates + spec.exit_gates) <= set(GATES), spec.id
    assert [s.name for s in list_scenarios()] == list(SCENARIOS)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_graph_shape_parallelism_and_safety_settings(name):
    by_id = {s.id: s for s in get_scenario(name).graph}
    impls = IMPLS[name]
    assert {by_id[i].parallel_group for i in impls} == {"impl"}
    assert len({tuple(by_id[i].depends_on) for i in impls}) == 1  # started by the same stage: they run together
    assert by_id["sync"].depends_on == impls and by_id["sync"].join == "all"
    assert by_id["testing"].depends_on == by_id["documentation"].depends_on == ["sync"]
    testing = by_id["testing"]
    assert (testing.max_attempts, testing.fallback_agent, testing.rollback_agent) == (
        3, "testing_fallback", "rollback_testing")
    release = by_id["release"]
    assert release.depends_on == ["testing", "documentation"]
    assert release.requires_approval and release.approval_kind == "production_release"
    assert release.autonomy is not Autonomy.FULL
    consumed = {n for spec in by_id.values() for n in spec.consumes}
    assert {"implementation", "test_report", "documentation", *impls} <= consumed


def test_approval_gates_of_the_brownfield_and_ambiguous_graphs():
    brown = {s.id: s for s in get_scenario("brownfield").graph}
    assert brown["api_change_review"].approval_kind == "public_api_change"
    assert brown["api_change_review"].requires_approval
    assert brown["api_change_review"].depends_on == ["architecture"]
    assert all(brown[i].depends_on == ["api_change_review"] for i in IMPLS["brownfield"])
    amb = {s.id: s for s in get_scenario("ambiguous").graph}
    assert amb["apply_interpretation"].approval_kind == "requirement_interpretation"
    assert amb["apply_interpretation"].requires_approval and "ambiguity_resolved" in amb["apply_interpretation"].exit_gates


# -------------------------------------------------------------------- happy paths
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_every_scenario_succeeds_with_auto_approve(name):
    run = await start(make_engine(auto_approve=True), name)
    assert run.state is R.SUCCEEDED, {i: (s.state.value, s.error) for i, s in run.stages.items()}
    assert set(states(run).values()) == {S.SUCCEEDED}
    assert {"requirements", "architecture", "implementation", "test_report", "documentation",
            "release_checklist"} <= set(run.artifacts)
    assert run.metrics.replans == 0 and run.metrics.retries == 0
    assert all(a.actor == "auto-approver" for a in run.approvals.values())


async def test_greenfield_artifacts_are_grounded():
    run = await start(make_engine(auto_approve=True), "greenfield")
    impl = run.artifacts["implementation"].content
    assert impl["status"] == "ok" and impl["verified_parts"] == ["analytics", "api", "store"]
    assert run.artifacts["release_checklist"].content["decision"] == "go"
    assert run.artifacts["test_report"].content["passed"] > 0


async def test_ambiguous_artifacts_say_what_is_design_only():
    run = await start(make_engine(auto_approve=True), "ambiguous")
    impl = run.artifacts["implementation"].content
    assert impl["verified_parts"] == ["ssrf"] and impl["design_only_parts"] == ["audit", "retention"]
    risks = run.artifacts["release_checklist"].content["open_risks"]
    assert any("'audit' is design only" in r for r in risks)


# --------------------------------------------------------- human in the loop
async def test_brownfield_pauses_at_api_review_then_release_and_resumes_on_new_engines():
    store = InMemoryRunStore()
    run = await start(make_engine(store), "brownfield")
    assert run.state is R.WAITING_APPROVAL
    (approval,) = pending(run)
    assert (approval.stage_id, approval.kind) == ("api_change_review", "public_api_change")
    assert [i for i, st in states(run).items() if st is S.SUCCEEDED] == ["requirements", "impact", "architecture"]
    assert all(states(run)[i] is S.PENDING for i in IMPLS["brownfield"])  # nothing downstream ran

    run = await make_engine(store).approve(run.id, approval.id, "alice")      # a brand-new engine resumes it
    assert run.state is R.WAITING_APPROVAL
    (release,) = pending(run)
    assert (release.stage_id, release.kind) == ("release", "production_release")
    assert states(run)["testing"] is S.SUCCEEDED and states(run)["release"] is S.WAITING_APPROVAL

    run = await make_engine(store).approve(run.id, release.id, "bob")
    assert run.state is R.SUCCEEDED
    assert {a.actor for a in run.approvals.values()} == {"alice", "bob"}
    assert run.metrics.approvals_requested == 2


async def test_rejecting_the_api_review_stops_the_delivery():
    engine = make_engine()
    run = await start(engine, "brownfield")
    run = await engine.reject(run.id, pending(run)[0].id, "alice", "too risky")
    assert run.state is R.FAILED
    assert states(run)["api_change_review"] is S.FAILED and "too risky" in run.stages["api_change_review"].error
    assert states(run)["release"] is S.SKIPPED and "release_checklist" not in run.artifacts


async def test_ambiguous_blocks_on_interpretation_until_a_human_approves():
    store = InMemoryRunStore()
    run = await start(make_engine(store), "ambiguous")
    assert run.state is R.WAITING_APPROVAL
    (approval,) = pending(run)
    assert (approval.stage_id, approval.kind) == ("apply_interpretation", "requirement_interpretation")
    assert states(run)["architecture"] is S.PENDING
    open_questions = run.artifacts["normalized_requirements"].content["open_questions"]
    assert len(open_questions) >= 3 and all(q["status"] == "proposed" and not q["resolution"] for q in open_questions)
    assert not gate_on(run, "ambiguity_resolved").passed

    run = await make_engine(store).approve(run.id, approval.id, "alice", "interpretation is fine")
    assert gate_on(run, "ambiguity_resolved").passed
    assert run.artifacts["normalized_requirements"].version == 2
    assert run.state is R.WAITING_APPROVAL and pending(run)[0].stage_id == "release"
    run = await make_engine(store).approve(run.id, pending(run)[0].id, "alice")
    assert run.state is R.SUCCEEDED


async def test_the_ambiguity_gate_cannot_be_skipped():
    """Remove the clarification steps: architecture's own entry gate still refuses the vague requirement."""
    graph = [s.model_copy(deep=True) for s in get_scenario("ambiguous").graph
             if s.id not in ("normalize", "apply_interpretation")]
    next(s for s in graph if s.id == "architecture").depends_on = ["requirements"]
    engine = make_engine(auto_approve=True)
    run = await engine.run(create(engine, "ambiguous", graph=graph).id)
    assert run.state is R.FAILED
    assert states(run)["architecture"] is S.BLOCKED
    assert "ambiguity_resolved" in run.stages["architecture"].error
    assert states(run)["impl_ssrf"] is S.SKIPPED and states(run)["release"] is S.SKIPPED


async def test_rejecting_the_interpretation_fails_the_run_before_any_design():
    engine = make_engine()
    run = await start(engine, "ambiguous")
    run = await engine.reject(run.id, pending(run)[0].id, "alice", "not what we meant")
    assert run.state is R.FAILED and "architecture" not in run.artifacts
    assert states(run)["architecture"] is S.SKIPPED


async def test_stop_while_paused_is_a_safe_stop():
    engine = make_engine()
    run = await start(engine, "brownfield")
    run = engine.stop(run.id)
    assert run.state is R.STOPPED
    assert states(run)["api_change_review"] is S.SKIPPED and states(run)["release"] is S.SKIPPED
    assert states(run)["impact"] is S.SUCCEEDED  # finished work is kept
    assert all(a.status.value != "pending" for a in run.approvals.values())


async def test_stop_before_start_skips_everything():
    engine = make_engine()
    run = engine.stop(create(engine, "greenfield").id)
    assert run.state is R.STOPPED and set(states(run).values()) == {S.SKIPPED}


# ------------------------------------------------------------ failure handling
async def test_transient_failure_is_retried_with_backoff_and_succeeds():
    sleeper = Sleeper()
    run = await start(make_engine(sleeper=sleeper, auto_approve=True), "greenfield", inject_failure="testing")
    assert run.state is R.SUCCEEDED
    testing = run.stages["testing"]
    assert testing.attempts == 2 and not testing.used_fallback
    assert run.metrics.retries >= 1 and run.metrics.fallbacks == 0
    assert sleeper.delays == [0.05]
    assert any(d.kind == "retry" and "injected transient" in d.rationale for d in run.decisions)


async def test_persistent_failure_uses_the_fallback_agent():
    run = await start(make_engine(auto_approve=True), "brownfield", inject_failure="testing_hard")
    assert run.state is R.SUCCEEDED
    testing = run.stages["testing"]
    assert testing.used_fallback and testing.attempts == 4  # 3 primary attempts + 1 fallback
    assert run.metrics.fallbacks == 1 and run.metrics.retries == 2
    assert run.artifacts["test_report"].content["suites"][0]["name"].startswith("fallback")
    assert any(d.kind == "fallback" for d in run.decisions)


async def test_total_failure_rolls_back_and_the_release_never_runs():
    run = await start(make_engine(auto_approve=True), "greenfield", inject_failure="testing_all")
    assert run.state is R.ROLLED_BACK
    assert states(run)["testing"] is S.ROLLED_BACK
    assert states(run)["release"] is S.SKIPPED and states(run)["documentation"] is S.SUCCEEDED
    assert "test_report" not in run.artifacts and "release_checklist" not in run.artifacts
    assert run.metrics.rollbacks == 1 and run.metrics.fallbacks == 1
    assert any(d.kind == "rollback" and "rollback_testing" in d.rationale for d in run.decisions)


async def test_failed_implementation_part_fails_sync_and_skips_the_rest(monkeypatch):
    from shortener import validation
    monkeypatch.setattr(validation, "validate_ttl", lambda ttl: ttl)  # a regression in the real code
    run = await start(make_engine(auto_approve=True), "brownfield")
    assert run.state is R.FAILED
    assert states(run)["sync"] is S.FAILED and "ttl" in run.stages["sync"].error
    assert states(run)["testing"] is S.SKIPPED and states(run)["release"] is S.SKIPPED


# ------------------------------------------------------- observability / audit
async def test_metrics_and_telemetry_are_consistent():
    telemetry = Telemetry()
    run = await start(make_engine(telemetry=telemetry, auto_approve=True), "greenfield", inject_failure="testing")
    m = run.metrics
    assert m.e2e_latency_ms > 0 and m.stages_succeeded == 9 and m.stages_failed == 0
    assert m.approvals_requested == 1 and m.retries == 1
    snap = telemetry.snapshot()
    assert snap["runs_started"] == 1 and snap["runs_succeeded"] == 1 and snap["success_rate"] == 1.0
    assert snap["retries_total"] == 1 and snap["mttr_ms"] is not None
    kinds = {e["event"] for e in telemetry.events_for(run.id)}
    assert {"start", "stage", "gate", "artifact", "approval", "retry", "finish"} <= kinds


@pytest.mark.parametrize("name, params", [("greenfield", {}),
                                          ("brownfield", {"inject_failure": "testing_all"}),
                                          ("ambiguous", {})])
async def test_every_decision_has_a_rationale(name, params):
    run = await start(make_engine(auto_approve=True), name, **params)
    assert len(run.decisions) > 8
    for d in run.decisions:
        assert d.rationale.strip() and d.outcome and d.kind and d.run_id == run.id, d
    gates = [d for d in run.decisions if d.kind == "gate"]
    assert gates and all(d.inputs["gate"] in GATES and d.inputs["phase"] in ("entry", "exit") for d in gates)


async def test_every_published_artifact_passes_policy():
    run = await start(make_engine(auto_approve=True), "ambiguous")
    policy = PolicyEngine()
    for name, artifact in run.artifacts.items():
        policy.check_artifact(name, artifact.content)


# ---------------------------------------------------------------- parallelism
def overlap(run, ids):
    """Concurrent = every stage started before any of them finished. Checked twice: on the wall-clock
    stamps (<= because the Windows clock ticks coarsely) and on the order of the decision log, which
    cannot tie."""
    spans = [(run.stages[i].started_at, run.stages[i].ended_at) for i in ids]
    log = [(d.stage_id, d.outcome) for d in run.decisions if d.kind == "stage" and d.stage_id in ids]
    last_start = max(i for i, e in enumerate(log) if e[1] == "started")
    first_end = min(i for i, e in enumerate(log) if e[1] in ("succeeded", "failed"))
    return max(s for s, _ in spans) <= min(e for _, e in spans) and last_start < first_end


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_parallel_stages_overlap_in_time(name):
    run = await start(make_engine(auto_approve=True), name)
    assert overlap(run, IMPLS[name]), "impl stages should run concurrently"
    assert overlap(run, ["testing", "documentation"]), "testing and documentation should run concurrently"
    sync = run.stages["sync"]
    assert sync.started_at >= max(run.stages[i].ended_at for i in IMPLS[name])  # the join waits for all


# ------------------------------------------------------------------ slow path
@pytest.mark.slow
async def test_real_pytest_subprocess_path_once():
    run = await start(make_engine(auto_approve=True), "greenfield", skip_pytest=False)
    assert run.state is R.SUCCEEDED
    report = run.artifacts["test_report"].content
    suite = next(s for s in report["suites"] if s["kind"] == "pytest")
    assert suite["exit_code"] == 0 and suite["failed"] == 0 and suite["passed"] > 100
    assert report["passed"] >= suite["passed"] + 6
