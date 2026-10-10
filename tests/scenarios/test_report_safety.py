"""What the approver reads must be (a) everything the approval applies and (b) free of terminal control codes."""
import json
import re

from scenarios.cli import main
from scenarios.registry import get_scenario
from scenarios.report import (
    format_run, format_trace, pending_approvals, resume_hint, review_lines, safe, table,
)

from .helpers import make_engine

ESC_TEXT = "\x1b[2K\r\x1b]0;pwned\x07 fake line\x9b31m\x08"
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


async def paused_ambiguous():
    engine = make_engine()
    scenario = get_scenario("ambiguous", llm=False)
    run = engine.create_run("ambiguous", scenario.requirement, scenario.graph, {"skip_pytest": True})
    run = await engine.run(run.id)
    (approval,) = pending_approvals(run)
    return run, approval


def test_safe_replaces_control_characters_with_visible_escapes_and_keeps_newline_and_tab():
    assert safe("a\x1b[2Kb\rc") == "a\\x1b[2Kb\\x0dc"
    assert safe("keep\nthis\tone") == "keep\nthis\tone"
    assert safe(ESC_TEXT) == safe(safe(ESC_TEXT)) and not CONTROL.search(safe(ESC_TEXT))
    assert not CONTROL.search(table(["h\x1b"], [["c\r1"]]))


async def test_the_approver_sees_the_feature_each_resolution_adds_and_the_rationale():
    run, approval = await paused_ambiguous()
    text = "\n".join(review_lines(run, approval))
    questions = run.artifacts["normalized_requirements"].content["open_questions"]
    assert questions and all(q["adds_feature"] for q in questions[:3])
    for q in questions:
        assert f"adds feature: {q['adds_feature'] or '(none)'}" in text
        assert f"rationale: {q['rationale']}" in text and q["proposed_resolution"] in text
    # what is shown is what is applied: approving adds exactly these features to the requirements
    added = {q["adds_feature"] for q in questions if q["adds_feature"]}
    applied = (await make_engine_approved(run, approval)).artifacts["normalized_requirements"].content
    assert added <= set(applied["detected_features"])


async def make_engine_approved(run, approval):
    """Approve on a fresh engine over the same run (the real flow after a restart)."""
    engine = make_engine()
    engine.store.save_run(run)
    return await engine.approve(run.id, approval.id, "alice")


async def test_an_empty_interpretation_is_flagged_instead_of_shown_as_a_blank_review():
    run, approval = await paused_ambiguous()
    run.artifacts["normalized_requirements"].content["open_questions"] = []
    lines = review_lines(run, approval)
    assert len(lines) == 1 and "NOTHING TO REVIEW" in lines[0] and "empty interpretation" in lines[0]
    assert "NOTHING TO REVIEW" in resume_hint(run, "db", "db")


async def test_no_report_function_ever_prints_a_control_character():
    run, approval = await paused_ambiguous()
    content = run.artifacts["normalized_requirements"].content
    content["open_questions"][0].update(question=ESC_TEXT, proposed_resolution=ESC_TEXT, rationale=ESC_TEXT,
                                        adds_feature=ESC_TEXT)
    content["generator"] = ESC_TEXT
    run.requirement = ESC_TEXT
    run.params["llm"] = True
    run.stages["apply_interpretation"].error = ESC_TEXT
    run.decisions[0].rationale = ESC_TEXT
    run.artifacts["normalized_requirements"].produced_by = ESC_TEXT
    for out in (format_run(run), format_trace(run), resume_hint(run, "d\x1bb", "db"), "\n".join(review_lines(run, approval))):
        assert not CONTROL.search(out)
    assert "\\x1b[2K" in format_run(run) and "\\x1b[2K" in "\n".join(review_lines(run, approval))


def test_the_cli_neutralises_control_characters_in_the_requirement_and_in_errors(tmp_path, capsys):
    db, audit = str(tmp_path / "runs.db"), str(tmp_path / "audit.jsonl")
    code = main(["ambiguous", "--skip-pytest", "--requirement", "Make links safer \x1b[2K\r fake line",
                 "--db", db, "--audit", audit])
    out = capsys.readouterr().out
    assert code == 0 and not CONTROL.search(out) and "\\x1b[2K" in out and "WAITING_APPROVAL" in out
    rid = re.search(r"run-[0-9a-f]{8}", out).group(0)

    code = main(["amend", rid, "--requirement", "Make links safer\x1b[31m and private", "--actor", "me", "--db", db,
                 "--audit", audit])
    out = capsys.readouterr().out
    assert code == 0 and not CONTROL.search(out)

    code = main(["status", "run-\x1b[2Kmissing", "--db", db, "--audit", audit])
    err = capsys.readouterr().err
    assert code == 2 and not CONTROL.search(err) and "unknown run" in err

    code = main(["status", rid, "--json", "--db", db, "--audit", audit])   # JSON escapes control characters itself
    assert code == 0 and json.loads(capsys.readouterr().out)["run_id"] == rid


def test_the_cli_refuses_a_requirement_with_a_secret_cleanly(tmp_path, capsys):
    db, audit = str(tmp_path / "runs.db"), str(tmp_path / "audit.jsonl")
    code = main(["greenfield", "--skip-pytest", "--requirement", "keep a log, password=hunter2hunter2", "--db", db, "--audit", audit])
    cap = capsys.readouterr()
    assert code == 2 and cap.out == ""
    assert "policy violation" in cap.err and "secret_detected" in cap.err and "hunter2hunter2" not in cap.err
    assert "Traceback" not in cap.err
    assert main(["status", "--db", db, "--audit", audit]) == 0 and "run-" not in capsys.readouterr().out  # no run created


def test_the_cli_refuses_a_comment_with_a_secret_and_leaves_the_approval_pending(tmp_path, capsys):
    db, audit = str(tmp_path / "runs.db"), str(tmp_path / "audit.jsonl")
    main(["brownfield", "--skip-pytest", "--db", db, "--audit", audit])
    rid = re.search(r"run-[0-9a-f]{8}", capsys.readouterr().out).group(0)
    code = main(["resume", rid, "--approve", "api_change_review-1", "--actor", "alice", "--comment",
                 "token=hunter2hunter2", "--db", db, "--audit", audit])
    cap = capsys.readouterr()
    assert code == 2 and "policy violation" in cap.err and "hunter2hunter2" not in cap.err
    assert main(["status", rid, "--db", db, "--audit", audit]) == 0
    assert "WAITING_APPROVAL" in capsys.readouterr().out
