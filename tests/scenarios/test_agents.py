"""Each agent on its own: its output must satisfy the gate that guards it, and be grounded in real code."""
import subprocess
from pathlib import Path

import pytest

from orchestrator.agents import AGENTS, build_agent_registry
from orchestrator.agents.common import PROJECT_ROOT
from orchestrator.agents.documentation import run_docs_dir
from orchestrator.agents.testing import run_pytest
from orchestrator.gates import GATES
from orchestrator.models import AgentContext, GateContext
from orchestrator.policy import PolicyEngine
from scenarios import ambiguous, brownfield, greenfield
from scenarios.registry import SCENARIOS, get_scenario
from shortener import validation

AMBIGUOUS = ambiguous.REQUIREMENT
BROWNFIELD = brownfield.REQUIREMENT
GREENFIELD = greenfield.REQUIREMENT


def ctx(requirement="", artifacts=None, stage_id="stage", attempt=1, is_fallback=False, **params):
    params.setdefault("skip_pytest", True)
    return AgentContext(run_id="run-unit", stage_id=stage_id, scenario="unit", requirement=requirement,
                        attempt=attempt, is_fallback=is_fallback, artifacts=artifacts or {}, params=params)


def gate(name, artifacts):
    return GATES[name](GateContext(run_id="r", stage_id="s", scenario="unit", artifacts=artifacts))


def produce(agent, **kw):
    """Run one agent and return the artifacts it publishes."""
    return AGENTS[agent](ctx(**kw)).artifacts


def requirements_for(text):
    return produce("requirements", requirement=text)["requirements"]


# ---------------------------------------------------------------- requirements
@pytest.mark.parametrize("text, expected", [
    (GREENFIELD, {"ttl", "analytics", "rate_limit"}),
    (BROWNFIELD, {"alias", "ttl", "rate_limit"}),
    ("Block localhost and private IP destinations (SSRF) and add audit logging", {"ssrf", "audit"}),
])
def test_clear_requirements_are_structured_and_not_ambiguous(text, expected):
    req = requirements_for(text)
    assert expected <= set(req["detected_features"])
    assert req["open_questions"] == []
    assert not req["ambiguity"]["is_ambiguous"]
    assert gate("requirements_complete", {"requirements": req}).passed
    assert gate("ambiguity_resolved", {"requirements": req}).passed
    assert all("Given" in c and "When" in c and "Then" in c for c in req["acceptance_criteria"][:-1])
    assert req["goals"] and req["non_goals"]


def test_vague_requirement_is_flagged_with_unresolved_questions():
    req = requirements_for("Make links safer")
    assert req["detected_features"] == []
    assert req["ambiguity"]["is_ambiguous"] and "safer" in req["ambiguity"]["vague_terms"]
    assert len(req["open_questions"]) >= 3
    assert all(q["resolution"] == "" for q in req["open_questions"])
    assert gate("requirements_complete", {"requirements": req}).passed    # structured, but...
    assert not gate("ambiguity_resolved", {"requirements": req}).passed   # ...not resolved


def test_vague_requirement_without_security_words_still_gets_questions():
    req = requirements_for("Make it better")
    assert len(req["open_questions"]) == 2 and not gate("ambiguity_resolved", {"requirements": req}).passed


def test_normalize_proposes_but_does_not_resolve():
    req = requirements_for(AMBIGUOUS)
    norm = produce("normalize", artifacts={"requirements": req})["normalized_requirements"]
    assert len(norm["open_questions"]) == len(req["open_questions"])
    for q in norm["open_questions"]:
        assert q["status"] == "proposed" and q["proposed_resolution"] and q["rationale"]
        assert q["resolution"] == ""
    assert not gate("ambiguity_resolved", {"requirements": req, "normalized_requirements": norm}).passed


def test_apply_interpretation_resolves_and_adds_features():
    req = requirements_for(AMBIGUOUS)
    norm = produce("normalize", artifacts={"requirements": req})["normalized_requirements"]
    done = produce("apply_interpretation", artifacts={"normalized_requirements": norm})["normalized_requirements"]
    assert all(q["status"] == "resolved" and q["resolution"] for q in done["open_questions"])
    assert {"ssrf", "audit", "retention"} <= set(done["detected_features"])
    assert done["interpretation_status"] == "resolved"
    assert len(done["acceptance_criteria"]) > len(norm["acceptance_criteria"])
    assert gate("ambiguity_resolved", {"requirements": req, "normalized_requirements": done}).passed


# ---------------------------------------------------------------- architecture
def test_architecture_derives_components_and_adrs_from_features():
    req = requirements_for(BROWNFIELD)
    arch = produce("architecture", artifacts={"requirements": req})["architecture"]
    names = {c["name"] for c in arch["components"]}
    assert {"api", "service", "store", "validation", "alias_policy", "expiry_policy", "rate_limiter"} <= names
    assert gate("architecture_present", {"architecture": arch}).passed
    assert arch["api_deltas"] and arch["data_model"]
    for adr in arch["decisions"]:
        assert adr["id"].startswith("ADR-") and adr["context"] and adr["decision"] and adr["consequence"]


def test_architecture_marks_what_already_exists_when_impact_is_known():
    req = requirements_for(BROWNFIELD)
    impact = produce("impact", artifacts={"requirements": req})["impact"]
    arch = produce("architecture", artifacts={"requirements": req, "impact": impact})["architecture"]
    assert [c["name"] for c in arch["components"] if c["new"]] == []  # alias, ttl, rate limit all exist already


# ---------------------------------------------------------------------- impact
def test_impact_cites_real_files_and_traces_the_call_path():
    req = requirements_for(BROWNFIELD)
    impact = produce("impact", artifacts={"requirements": req})["impact"]
    assert gate("impact_mapped", {"impact": impact}).passed
    for module in impact["impacted_modules"]:
        assert (PROJECT_ROOT / module["file"]).is_file(), module
        assert module["functions"] and module["features"]
    traces = [t["trace"] for t in impact["call_traces"]]
    assert ("POST /api/v1/urls -> app.create_url -> service.UrlService.create -> validation.validate_alias"
            in traces)
    assert any(t.endswith("app.enforce_rate_limit -> ratelimit.RateLimiter.check") for t in traces)
    assert impact["requires_approval"] and impact["approval_kind"] == "public_api_change"
    assert all(f["implemented"] for f in impact["existing_support"])
    assert impact["regression_risks"] and "tests/shortener/test_api.py" in impact["tests_to_rerun"]


def test_impact_with_no_codebase_maps_nothing_and_fails_its_gate(tmp_path):
    req = requirements_for(BROWNFIELD)
    impact = produce("impact", artifacts={"requirements": req}, shortener_root=str(tmp_path))["impact"]
    assert impact["impacted_modules"] == []
    assert not gate("impact_mapped", {"impact": impact}).passed


def test_api_change_review_records_what_was_approved():
    req = requirements_for(BROWNFIELD)
    impact = produce("impact", artifacts={"requirements": req})["impact"]
    review = produce("api_change_review", artifacts={"impact": impact})["api_change_review"]
    assert review["approved_changes"] and review["backward_compatible"]


# -------------------------------------------------------------- implementation
@pytest.mark.parametrize("agent", ["impl_api", "impl_store", "impl_analytics", "impl_alias", "impl_ttl",
                                   "impl_ratelimit", "impl_ssrf"])
def test_verifying_agents_run_real_code_and_it_checks_out(agent):
    part = produce(agent)[agent]
    assert part["status"] == "ok" and part["verified"] and not part["design_only"]
    assert len(part["evidence"]) >= 6
    for e in part["evidence"]:
        assert set(e) == {"check", "input", "expected", "actual", "ok"} and e["ok"], e


@pytest.mark.parametrize("agent", ["impl_audit", "impl_retention"])
def test_design_only_agents_are_honest_about_it(agent):
    part = produce(agent)[agent]
    assert part["design_only"] and part["verified"] is False
    assert part["status"] == "ok" and part["design"]["not_built"]
    assert all(e["ok"] for e in part["evidence"])


def test_a_regression_in_the_real_code_fails_the_part(monkeypatch):
    monkeypatch.setattr(validation, "validate_alias", lambda alias: alias)  # accepts everything
    part = produce("impl_alias")["impl_alias"]
    assert part["status"] == "failed" and not part["verified"]
    assert any(not e["ok"] for e in part["evidence"])


def test_sync_merges_parts_into_implementation():
    parts = {n: produce(n)[n] for n in ("impl_ssrf", "impl_audit", "impl_retention")}
    impl = produce("sync", artifacts=parts)["implementation"]
    assert gate("implementation_complete", {"implementation": impl}).passed
    assert impl["verified_parts"] == ["ssrf"] and impl["design_only_parts"] == ["audit", "retention"]
    assert set(impl["parts"]) == {"ssrf", "audit", "retention"}


def test_sync_raises_when_any_part_failed(monkeypatch):
    monkeypatch.setattr(validation, "validate_ttl", lambda ttl: ttl)
    parts = {n: produce(n)[n] for n in ("impl_alias", "impl_ttl")}
    with pytest.raises(RuntimeError, match="ttl"):
        produce("sync", artifacts=parts)


# --------------------------------------------------------------------- testing
def test_testing_agent_runs_a_live_smoke_test_and_reports():
    report = produce("testing")["test_report"]
    assert gate("tests_green", {"test_report": report}).passed
    smoke = next(s for s in report["suites"] if s["kind"] == "smoke")
    assert smoke["passed"] >= 6 and smoke["failed"] == 0 and report["duration_ms"] > 0
    assert not any(s["kind"] == "pytest" for s in report["suites"])  # skip_pytest


def test_transient_injection_fails_only_the_first_attempt():
    with pytest.raises(RuntimeError, match="transient"):
        AGENTS["testing"](ctx(inject_failure="testing", attempt=1))
    assert AGENTS["testing"](ctx(inject_failure="testing", attempt=2)).artifacts["test_report"]["failed"] == 0


def test_hard_injection_fails_every_primary_attempt_but_not_the_fallback():
    for attempt in (1, 2, 3):
        with pytest.raises(RuntimeError):
            AGENTS["testing"](ctx(inject_failure="testing_hard", attempt=attempt))
    report = AGENTS["testing_fallback"](ctx(inject_failure="testing_hard", attempt=4, is_fallback=True))
    assert gate("tests_green", report.artifacts).passed
    assert report.artifacts["test_report"]["suites"][0]["name"].startswith("fallback")


def test_total_injection_fails_the_fallback_too():
    with pytest.raises(RuntimeError, match="fallback"):
        AGENTS["testing_fallback"](ctx(inject_failure="testing_all", is_fallback=True))


def test_rollback_testing_says_what_it_undid():
    result = AGENTS["rollback_testing"](ctx(failure="boom"))
    assert "boom" in result.summary and "release gate stays closed" in result.summary


def test_pytest_summary_line_is_parsed(monkeypatch):
    def fake_run(cmd, **kw):
        assert cmd[1:3] == ["-m", "pytest"] and "tests/shortener" in cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="..F\n2 failed, 120 passed in 4.2s\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    suite = run_pytest(Path("."))
    assert (suite["passed"], suite["failed"]) == (120, 2)


def test_unreadable_pytest_output_never_counts_as_green(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="x"))
    assert run_pytest(Path("."))["failed"] == 1


# --------------------------------------------------- documentation and release
def sample_artifacts():
    req = requirements_for(GREENFIELD)
    arch = produce("architecture", artifacts={"requirements": req})["architecture"]
    parts = {n: produce(n)[n] for n in ("impl_api", "impl_store")}
    impl = produce("sync", artifacts=parts)["implementation"]
    return {"requirements": req, "architecture": arch, "implementation": impl}


def test_documentation_writes_real_files_only_under_docs_generated(tmp_path):
    result = produce("documentation", artifacts=sample_artifacts(), shortener_root=str(tmp_path))
    docs = result["documentation"]
    assert gate("docs_present", result).passed and len(docs["files"]) == 3
    assert "POST /api/v1/urls" in docs["endpoints_documented"]
    folder = run_docs_dir(tmp_path, "run-unit")
    assert {p.name for p in folder.iterdir()} == {"CHANGELOG.md", "API.md", "DESIGN.md"}
    assert "/api/v1/urls/{code}/analytics" in (folder / "API.md").read_text(encoding="utf-8")
    assert "ADR-001" in (folder / "DESIGN.md").read_text(encoding="utf-8")
    assert all(f["path"].startswith("docs/generated/run-unit/") for f in docs["files"])
    assert {p.name for p in tmp_path.iterdir()} == {"docs"}


def test_a_hostile_run_id_cannot_escape_the_docs_folder(tmp_path):
    assert run_docs_dir(tmp_path, "../../etc").parent == tmp_path / "docs" / "generated"


def release_artifacts(tmp_path, failed=0):
    arts = sample_artifacts()
    arts["documentation"] = produce("documentation", artifacts=arts, shortener_root=str(tmp_path))["documentation"]
    arts["test_report"] = {"passed": 10, "failed": failed, "suites": [], "duration_ms": 1}
    return arts


def test_release_builds_a_go_checklist_and_rollback_undoes_the_file(tmp_path):
    arts = release_artifacts(tmp_path)
    checklist = AGENTS["release"](ctx(artifacts=arts, shortener_root=str(tmp_path))).artifacts["release_checklist"]
    assert checklist["decision"] == "go" and all(c["ok"] for c in checklist["checks"])
    assert checklist["rollback_plan"] and checklist["open_risks"]
    assert "production_release (this stage)" in checklist["approvals_required"]
    file = run_docs_dir(tmp_path, "run-unit") / "RELEASE_CHECKLIST.md"
    assert file.exists()
    summary = AGENTS["rollback_release"](ctx(shortener_root=str(tmp_path), failure="x")).summary
    assert not file.exists() and "removed" in summary


def test_release_is_no_go_when_tests_failed(tmp_path):
    with pytest.raises(RuntimeError, match="no-go"):
        AGENTS["release"](ctx(artifacts=release_artifacts(tmp_path, failed=1), shortener_root=str(tmp_path)))


# --------------------------------------------------------- registry and policy
def test_registry_covers_every_agent_the_scenarios_name():
    registry = build_agent_registry()
    for name in SCENARIOS:
        for spec in get_scenario(name).graph:
            for agent in (spec.agent, spec.fallback_agent, spec.rollback_agent):
                assert agent is None or agent in registry, (name, spec.id, agent)


def test_registry_root_is_a_default_that_params_override(tmp_path):
    other = tmp_path / "other"
    registry = build_agent_registry(tmp_path)
    registry["documentation"](ctx(artifacts=sample_artifacts()))
    assert run_docs_dir(tmp_path, "run-unit").is_dir()
    registry["documentation"](ctx(artifacts=sample_artifacts(), shortener_root=str(other)))
    assert run_docs_dir(other, "run-unit").is_dir()


def test_no_artifact_from_any_agent_contains_secrets_or_unsafe_urls(tmp_path):
    policy = PolicyEngine()
    arts = sample_artifacts()
    for name in ("impl_ssrf", "impl_audit", "impl_retention", "impl_alias", "impl_ttl", "impl_ratelimit"):
        arts.update(produce(name))
    arts.update(produce("normalize", artifacts={"requirements": requirements_for(AMBIGUOUS)}))
    arts.update(produce("impact", artifacts={"requirements": requirements_for(BROWNFIELD)}))
    arts.update(produce("testing"))
    arts.update(produce("documentation", artifacts=arts, shortener_root=str(tmp_path)))
    for name, content in arts.items():
        policy.check_artifact(name, content)  # raises PolicyViolation
    for page in run_docs_dir(tmp_path, "run-unit").iterdir():
        policy.check_artifact(page.name, {"text": page.read_text(encoding="utf-8")})
