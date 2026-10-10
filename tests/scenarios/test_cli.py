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

    run.audit = audit
    return run


def run_id(out: str) -> str:
    return re.search(r"run-[0-9a-f]{8}", out).group(0)


def test_greenfield_auto_approve_succeeds_and_prints_the_stage_table(cli):
    code, cap = cli("greenfield", "--auto-approve", "--skip-pytest")
    assert code == 0
    for phrase in ("SUCCEEDED", "STAGE", "ATTEMPTS", "GATES", "APPROVAL", "impl_analytics", "production_release",
                   "approved by auto-approver", "SHA256", "Metrics:", "e2e=", "retries=0"):
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
