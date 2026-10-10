"""CLI tests call main(argv) in-process. Each call builds a fresh Engine over the same SQLite file, which is
exactly what separate shell invocations do, so the resume tests also prove the state is durable."""
import json
import re

import pytest

from scenarios.cli import main


@pytest.fixture
def cli(tmp_path, capsys):
    db, audit = str(tmp_path / "runs.db"), str(tmp_path / "audit.jsonl")

    def run(*argv):
        code = main([*argv, "--db", db, "--audit", audit])
        return code, capsys.readouterr()

    run.audit, run.db = audit, db
    return run


def run_id(out: str) -> str:
    return re.search(r"run-[0-9a-f]{8}", out).group(0)


def test_greenfield_auto_approve_succeeds_and_prints_the_stage_table(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest")
    assert code == 0
    for phrase in ("SUCCEEDED", "STAGE", "ATTEMPTS", "GATES", "APPROVAL", "impl_analytics", "production_release",
                   "approved by demo-auto-approver", "DEMO MODE: all approvals auto-granted", "SHA256", "Metrics:", "e2e=", "retries=0"):
        assert phrase in cap.out, phrase


def test_audit_trail_is_written_as_jsonl(cli):
    cli("greenfield", "--auto-approve", "--skip-pytest")
    lines = [json.loads(line) for line in open(cli.audit, encoding="utf-8")]
    assert lines and {"ts", "run_id", "event", "details"} <= set(lines[0])


def test_without_auto_approve_it_pauses_and_explains_how_to_resume(cli):
    code, cap = cli("brownfield", "--skip-pytest")
    assert code == 0  # waiting for a human is not a failure
    assert "WAITING_APPROVAL" in cap.out and "PAUSED" in cap.out
    rid = run_id(cap.out)
    assert f"python -m scenarios resume {rid} --approve api_change_review-1 --actor you" in cap.out
    assert "public_api_change" in cap.out and "POST and PATCH accept ttl_seconds" in cap.out  # what you approve


def test_resume_walks_a_run_through_its_approvals_across_invocations(cli):
    rid = run_id(cli("brownfield", "--skip-pytest")[1].out)
    code, cap = cli("resume", rid, "--approve", "api_change_review-1", "--actor", "alice")
    assert code == 0 and "PAUSED" in cap.out and "release-1" in cap.out
    assert "public_api_change: approved by alice" in cap.out
    code, cap = cli("status", rid)
    assert code == 0 and "WAITING_APPROVAL" in cap.out
    code, cap = cli("resume", rid, "--approve", "release-1", "--actor", "bob")
    assert code == 0 and "SUCCEEDED" in cap.out and "PAUSED" not in cap.out


def test_ambiguous_shows_the_proposed_interpretations_before_approval(cli):
    code, cap = cli("ambiguous", "--skip-pytest")
    assert code == 0 and "requirement_interpretation" in cap.out
    assert "proposed:" in cap.out and "Destination safety first" in cap.out


def test_reject_fails_the_run_with_exit_code_1(cli):
    rid = run_id(cli("brownfield", "--skip-pytest")[1].out)
    code, cap = cli("resume", rid, "--reject", "api_change_review-1", "--actor", "alice", "--comment", "no")
    assert code == 1 and "FAILED" in cap.out and "approval rejected by alice: no" in cap.out


def test_injected_transient_failure_is_retried(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest", "--inject-failure", "testing")
    assert code == 0 and "SUCCEEDED" in cap.out and "retries=1" in cap.out


def test_hard_failure_uses_the_fallback(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest", "--inject-failure", "testing_hard")
    assert code == 0 and "(fallback)" in cap.out and "fallbacks=1" in cap.out


def test_total_failure_exits_1_and_skips_the_release(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest", "--inject-failure", "testing_all")
    assert code == 1
    assert "ROLLED_BACK" in cap.out and "rollbacks=1" in cap.out
    assert re.search(r"release\s+skipped", cap.out)


def test_json_output_is_machine_readable(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest", "--json")
    data = json.loads(cap.out)
    assert code == 0 and data["state"] == "succeeded" and data["pending_approvals"] == []
    assert [s["id"] for s in data["stages"]][0] == "requirements" and "test_report" in data["artifacts"]


def test_status_lists_runs_and_trace_and_metrics_read_the_store(cli):
    rid = run_id(cli("greenfield", "--auto-approve", "--skip-pytest")[1].out)
    cli("ambiguous", "--skip-pytest")
    code, cap = cli("status")
    assert code == 0 and rid in cap.out and "ambiguous" in cap.out and "apply_interpretation-1" in cap.out
    code, cap = cli("trace", rid)
    assert code == 0 and "RATIONALE" in cap.out and "stage succeeded" in cap.out and "approved" in cap.out
    code, cap = cli("metrics", rid)
    assert code == 0 and cap.out.startswith("Metrics:")
    code, cap = cli("metrics")
    assert code == 0 and "runs_started" in cap.out and "success_rate" in cap.out
    assert json.loads(cli("metrics", "--json")[1].out)["runs_succeeded"] == 1


def test_unknown_run_or_approval_is_a_clean_error(cli):
    code, cap = cli("status", "run-deadbeef")
    assert code == 2 and "unknown run" in cap.err
    rid = run_id(cli("brownfield", "--skip-pytest")[1].out)
    code, cap = cli("resume", rid, "--approve", "nope-1")
    assert code == 2 and "unknown approval" in cap.err


def test_unknown_scenario_is_rejected_by_the_parser():
    with pytest.raises(SystemExit) as exc:
        main(["nonsense"])
    assert exc.value.code == 2


def test_auto_approve_is_a_loud_demo_mode_with_a_machine_actor(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest")
    assert code == 0 and "DEMO MODE: all approvals auto-granted" in cap.out
    rid = run_id(cap.out)
    trace = cli("trace", rid, "--json")[1].out
    approved = [d for d in json.loads(trace) if d["kind"] == "approval" and d["outcome"] == "approved"]
    assert approved and {d["inputs"]["actor"] for d in approved} == {"demo-auto-approver"}
    assert any(d["inputs"]["high_impact"] for d in approved)  # production_release is flagged high impact


def test_banner_goes_to_stderr_with_json_so_stdout_stays_parseable(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest", "--json")
    assert "DEMO MODE" in cap.err and "DEMO MODE" not in cap.out
    assert json.loads(cap.out)["state"] == "succeeded"


def test_without_the_flag_there_is_no_banner_and_no_machine_approval(cli):
    code, cap = cli("brownfield", "--skip-pytest")
    assert "DEMO MODE" not in cap.out and "demo-auto-approver" not in cap.out and "WAITING_APPROVAL" in cap.out


def test_resume_refuses_reserved_actor_names(cli):
    rid = run_id(cli("brownfield", "--skip-pytest")[1].out)
    code, cap = cli("resume", rid, "--approve", "api_change_review-1", "--actor", "demo-auto-approver")
    assert code == 2 and "reserved" in cap.err


# ---------------------------------------------------------------- amend (re-plan)
ALIASES = "Extend the URL shortener: add custom aliases for links"
ALIASES_AND_EXPIRY = "Extend the URL shortener: add custom aliases and expiring links"


def paused_at_release(cli):
    """A brownfield run with its public API change approved, now waiting at the release gate."""
    rid = run_id(cli("brownfield", "--skip-pytest", "--requirement", ALIASES)[1].out)
    code, cap = cli("resume", rid, "--approve", "api_change_review-1", "--actor", "alice")
    assert code == 0 and "release-1" in cap.out
    return rid


def test_amend_shows_which_stages_re_ran_and_which_kept_their_results(cli):
    rid = paused_at_release(cli)
    code, cap = cli("amend", rid, "--requirement", ALIASES_AND_EXPIRY, "--actor", "alice", "--comment", "add expiry")
    assert code == 0, cap.err
    out = cap.out
    assert "Requirement amended by alice" in out and f'after:  "{ALIASES_AND_EXPIRY}"' in out
    assert "RUNS" in out and "RESULT" in out
    for stage in ("requirements", "impact", "architecture"):
        assert re.search(rf"^{stage}\s+succeeded\s+1 -> 2\s+re-ran", out, re.M), stage
    assert re.search(r"^testing\s+succeeded\s+1 -> 1\s+kept", out, re.M)  # nothing it consumes changed
    assert re.search(r"^api_change_review\s+waiting_approval\s+1 -> 1\s+waiting for approval", out, re.M)
    # the lineage written by this command, and the approval that must be given again on the new evidence
    assert "requirement_amended" in out and "requirement amended by alice: add expiry" in out
    assert re.search(r"approval\s+release\s+cancelled", out)
    assert "api_change_review-2" in out and "[ttl] POST and PATCH accept ttl_seconds" in out  # the NEW evidence
    assert "PAUSED" in out and re.search(r"replans=[1-9]", out)

    # the run is really continued from SQLite: approve again, then release again
    code, cap = cli("resume", rid, "--approve", "api_change_review-2", "--actor", "bob")
    assert code == 0 and "release-2" in cap.out and "RUNS" in cap.out
    assert "production_release: withdrawn by engine" in cap.out  # the old release approval, not a human 'no'
    assert re.search(r"^testing\s+succeeded\s+\d+\s+1\s+1\s", cap.out, re.M)       # kept: one execution in total
    assert re.search(r"^impl_ttl\s+succeeded\s+\d+\s+1\s+2\s", cap.out, re.M)      # re-ran on the new architecture
    code, cap = cli("resume", rid, "--approve", "release-2", "--actor", "carol")
    assert code == 0 and "SUCCEEDED" in cap.out


def test_amend_a_run_paused_at_the_first_approval_asks_for_a_fresh_one(cli):
    rid = run_id(cli("brownfield", "--skip-pytest", "--requirement", ALIASES)[1].out)
    code, cap = cli("amend", rid, "--requirement", ALIASES_AND_EXPIRY, "--actor", "alice")
    assert code == 0 and "api_change_review-2" in cap.out
    assert re.search(r"^impl_alias\s+pending\s+0 -> 0\s+not run yet", cap.out, re.M)
    code, cap = cli("resume", rid, "--approve", "api_change_review-1", "--actor", "alice")
    assert code == 2 and "already rejected" in cap.err  # the human's old decision cannot be replayed


def test_amend_json_output_is_machine_readable(cli):
    rid = paused_at_release(cli)
    code, cap = cli("amend", rid, "--requirement", ALIASES_AND_EXPIRY, "--actor", "alice", "--json")
    data = json.loads(cap.out)
    assert code == 0 and data["run_id"] == rid and data["pending_approvals"] == ["api_change_review-2"]
    amendment = data["amendment"]
    assert amendment["actor"] == "alice" and amendment["requirement"] == ALIASES_AND_EXPIRY
    assert amendment["previous_requirement"] == ALIASES
    results = {s["id"]: s["result"] for s in amendment["stages"]}
    assert results["requirements"] == "re-ran" and results["testing"].startswith("kept")
    assert any(d["outcome"] == "requirement_amended" for d in amendment["decisions"])
    assert data["metrics"]["replans"] >= 1


@pytest.mark.parametrize("extra, message", [
    (["--requirement", ALIASES, "--actor", "alice"], "identical"),
    (["--requirement", "   ", "--actor", "alice"], "non-empty"),
    (["--requirement", "x" * 2001, "--actor", "alice"], "limit"),
    (["--requirement", "new text", "--actor", "auto-approver"], "reserved"),
    (["--requirement", "new text", "--actor", "engine"], "reserved"),
])
def test_amend_refusals_are_clean_errors_with_exit_code_2(cli, extra, message):
    rid = run_id(cli("brownfield", "--skip-pytest", "--requirement", ALIASES)[1].out)
    code, cap = cli("amend", rid, *extra)
    assert code == 2 and message in cap.err and "Traceback" not in cap.err
    assert ALIASES in cli("status", rid)[1].out  # the requirement is unchanged


def test_amend_refuses_policy_violations_without_echoing_the_secret(cli):
    rid = run_id(cli("brownfield", "--skip-pytest", "--requirement", ALIASES)[1].out)
    code, cap = cli("amend", rid, "--requirement", 'add aliases, api_key = "sk-live-1234567890abcdef"', "--actor", "alice")
    assert code == 2 and "policy violation" in cap.err and "sk-live" not in cap.err + cap.out
    code, cap = cli("status", rid)
    assert "WAITING_APPROVAL" in cap.out and ALIASES in cap.out  # the run was left as it was
    code, cap = cli("trace", rid)
    assert "requirement amendment refused" in cap.out and "sk-live" not in cap.out


def test_amend_unknown_and_finished_runs_are_clean_errors(cli):
    code, cap = cli("amend", "run-deadbeef", "--requirement", "x y z", "--actor", "alice")
    assert code == 2 and "unknown run" in cap.err
    rid = run_id(cli("greenfield", "--auto-approve", "--skip-pytest")[1].out)
    code, cap = cli("amend", rid, "--requirement", "x y z", "--actor", "alice")
    assert code == 2 and "already succeeded" in cap.err


def test_amend_requires_a_requirement():
    with pytest.raises(SystemExit) as exc:
        main(["amend", "run-12345678"])
    assert exc.value.code == 2


# ------------------------------------------------- resume hint, --llm, warnings
def test_resume_hint_repeats_a_custom_db_and_audit_path(cli):
    code, cap = cli("brownfield", "--skip-pytest")
    rid = run_id(cap.out)
    flags = f"--db {cli.db} --audit {cli.audit}"
    for verb in (f"resume {rid} --approve api_change_review-1 --actor you", f"resume {rid} --reject api_change_review-1 --actor you",
                 f'amend {rid} --requirement "..." --actor you'):
        assert f"python -m scenarios {verb} {flags}" in cap.out, verb
    # the printed command really works against the same files
    code, cap = cli("status", rid)
    assert code == 0 and f"--audit {cli.audit}" in cap.out


def test_resume_hint_has_no_flags_for_the_default_paths(cli):
    from orchestrator.store import SqliteRunStore
    from scenarios.cli import DEFAULT_AUDIT, DEFAULT_DB
    from scenarios.report import resume_hint
    rid = run_id(cli("brownfield", "--skip-pytest")[1].out)
    store = SqliteRunStore(cli.db)
    try:
        run = store.load_run(rid)
    finally:
        store.close()
    plain = resume_hint(run, DEFAULT_DB, DEFAULT_DB, DEFAULT_AUDIT, DEFAULT_AUDIT)
    assert "--db" not in plain and "--audit" not in plain
    assert "--db" in resume_hint(run, "other.db", DEFAULT_DB, DEFAULT_AUDIT, DEFAULT_AUDIT)
    spaced = resume_hint(run, "my dir/a.db", DEFAULT_DB, "my dir/a.jsonl", DEFAULT_AUDIT)
    assert '--db "my dir/a.db" --audit "my dir/a.jsonl"' in spaced


def test_llm_flag_is_only_on_the_scenario_commands_and_forces_the_llm_variant(cli, monkeypatch):
    import scenarios.cli as cli_module
    seen = []
    real = cli_module.get_scenario

    def spy(name, llm=None):
        seen.append(llm)
        return real(name, llm=False)  # no network: the deterministic scenario, the call itself is what we test

    monkeypatch.setattr(cli_module, "get_scenario", spy)
    assert cli("greenfield", "--auto-approve", "--skip-pytest")[0] == 0
    assert cli("greenfield", "--auto-approve", "--skip-pytest", "--llm")[0] == 0
    assert seen == [None, True]
    for command in ("resume", "amend", "status"):
        with pytest.raises(SystemExit):
            main([command, "run-12345678", "--llm"])


def test_llm_flag_is_recorded_as_a_run_param(cli, monkeypatch):
    import scenarios.cli as cli_module
    real = cli_module.get_scenario
    monkeypatch.setattr(cli_module, "get_scenario", lambda name, llm=None: real(name, llm=False))
    rid = run_id(cli("brownfield", "--skip-pytest", "--llm")[1].out)
    from orchestrator.store import SqliteRunStore
    store = SqliteRunStore(cli.db)
    try:
        assert store.load_run(rid).params["llm"] is True
    finally:
        store.close()


def test_cli_prints_no_starlette_deprecation_warning(tmp_path):
    """`python -m scenarios ...` used to print a StarletteDeprecationWarning (httpx / testclient) on every command."""
    import subprocess
    import sys

    from orchestrator.agents.common import PROJECT_ROOT
    proc = subprocess.run([sys.executable, "-m", "scenarios", "status", "--db", str(tmp_path / "w.db"),
                           "--audit", str(tmp_path / "w.jsonl")], cwd=PROJECT_ROOT, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "Deprecat" not in proc.stderr and "Traceback" not in proc.stderr and proc.stderr.strip() == ""


def test_status_with_no_runs_prints_just_the_header(cli):
    code, cap = cli("status")
    assert code == 0 and "RUN" in cap.out and "PENDING APPROVALS" in cap.out
