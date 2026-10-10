from orchestrator.gates import GATES, run_gates
from orchestrator.models import GateContext, GateResult


def ctx(artifacts=None, stage_states=None, approvals=None, stage_id="s"):
    return GateContext(run_id="r", stage_id=stage_id, scenario="x", artifacts=artifacts or {},
                       stage_states=stage_states or {}, approvals=approvals or [])


def check(gate, c):
    return GATES[gate](c)


def test_every_gate_returns_a_message_and_names_itself():
    for name, fn in GATES.items():
        result = fn(ctx())
        assert result.gate == name and result.message


def test_requirements_complete():
    good = {"requirements": {"problem": "p", "acceptance_criteria": ["a"], "assumptions": ["x"]}}
    assert check("requirements_complete", ctx(good)).passed
    bad = {"requirements": {"problem": "p", "acceptance_criteria": [], "assumptions": ["x"]}}
    r = check("requirements_complete", ctx(bad))
    assert not r.passed and r.details["empty_fields"] == ["acceptance_criteria"]
    assert not check("requirements_complete", ctx()).passed


def test_ambiguity_resolved():
    resolved = {"requirements": {"open_questions": [{"question": "q", "resolution": "assume X"}]}}
    assert check("ambiguity_resolved", ctx(resolved)).passed
    none_open = {"requirements": {}}
    assert check("ambiguity_resolved", ctx(none_open)).passed
    open_q = {"requirements": {"open_questions": [{"question": "q1", "resolution": ""},
                                                  {"question": "q2", "resolution": "ok"}]}}
    r = check("ambiguity_resolved", ctx(open_q))
    assert not r.passed and r.details["unresolved"] == ["q1"]


def test_ambiguity_prefers_normalized_requirements():
    arts = {"requirements": {"open_questions": [{"question": "q", "resolution": ""}]},
            "normalized_requirements": {"open_questions": [{"question": "q", "resolution": "done"}]}}
    assert check("ambiguity_resolved", ctx(arts)).passed
    assert not check("ambiguity_resolved", ctx()).passed


def test_architecture_and_impact():
    assert check("architecture_present", ctx({"architecture": {"components": ["api"]}})).passed
    assert not check("architecture_present", ctx({"architecture": {"components": []}})).passed
    assert check("impact_mapped", ctx({"impact": {"impacted_modules": ["m"]}})).passed
    assert not check("impact_mapped", ctx({"impact": {"impacted_modules": []}})).passed


def test_implementation_complete():
    assert check("implementation_complete", ctx({"implementation": {"status": "ok"}})).passed
    assert not check("implementation_complete", ctx({"implementation": {"status": "partial"}})).passed


def test_tests_green():
    assert check("tests_green", ctx({"test_report": {"passed": 5, "failed": 0}})).passed
    assert not check("tests_green", ctx({"test_report": {"passed": 5, "failed": 1}})).passed
    assert not check("tests_green", ctx({"test_report": {"passed": 0, "failed": 0}})).passed  # empty suite
    assert not check("tests_green", ctx()).passed


def test_docs_present():
    assert check("docs_present", ctx({"documentation": {"files": ["README.md"]}})).passed
    assert not check("docs_present", ctx({"documentation": {"files": []}})).passed


def test_release_ready_needs_green_tests_and_no_bad_stage():
    green = {"test_report": {"passed": 3, "failed": 0}}
    assert check("release_ready", ctx(green, {"a": "succeeded", "release": "running"}, stage_id="release")).passed
    r = check("release_ready", ctx(green, {"a": "failed"}))
    assert not r.passed and r.details["bad_stages"] == ["a"]
    assert not check("release_ready", ctx({"test_report": {"passed": 3, "failed": 2}})).passed


def test_no_policy_exceptions():
    assert check("no_policy_exceptions", ctx()).passed
    pending = [{"id": "x-1", "kind": "security_exception", "status": "pending"}]
    assert not check("no_policy_exceptions", ctx(approvals=pending)).passed
    approved = [{"id": "x-1", "kind": "security_exception", "status": "approved"}]
    assert check("no_policy_exceptions", ctx(approvals=approved)).passed
    other = [{"id": "y-1", "kind": "schema_change", "status": "pending"}]
    assert check("no_policy_exceptions", ctx(approvals=other)).passed


def test_run_gates_fails_closed_on_unknown_gate():
    [r] = run_gates(["does_not_exist"], ctx())
    assert not r.passed and "unknown gate" in r.message


def test_run_gates_fails_closed_when_a_gate_raises():
    def boom(_):
        raise RuntimeError("kaput")
    [r] = run_gates(["boom"], ctx(), registry={"boom": boom})
    assert not r.passed and "kaput" in r.message


def test_run_gates_uses_custom_registry_and_keeps_order():
    reg = {"yes": lambda c: GateResult(gate="yes", passed=True),
           "no": lambda c: GateResult(gate="no", passed=False, message="nope")}
    results = run_gates(["no", "yes"], ctx(), registry=reg)
    assert [r.gate for r in results] == ["no", "yes"]
    assert [r.passed for r in results] == [False, True]
