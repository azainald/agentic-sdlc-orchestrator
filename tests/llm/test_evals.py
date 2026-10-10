"""The eval harness, scored against a ScriptedLLM (no network)."""
import json

import pytest

from evals.cases import CASES
from evals.cli import main
from evals.runner import FAMILIES, Throttled, evaluate, format_report, summary
from evals.score import prf, run_checked, score_requirements
from orchestrator.agents import build_agent_registry
from orchestrator.agents.features import FEATURES
from orchestrator.llm import LLMRateLimited, LLMUnavailable, ScriptedLLM
from orchestrator.llm.config import LLMSettings
from orchestrator.models import AgentContext

from .helpers import ARCH, INJECTION, NARRATIVE, NORMALIZE, REQ_CLEAR, REQ_VAGUE, SECRET

BY_ID = {c.id: c for c in CASES}


def modes(llm):
    return {"deterministic": (build_agent_registry(), ""), "llm": (build_agent_registry(llm=llm), "_llm")}


def ctx(text):
    return AgentContext(run_id="e", stage_id="requirements", scenario="eval", requirement=text, attempt=1)


# ---------------------------------------------------------------- the cases
def test_cases_are_labelled_with_real_features_and_cover_the_required_kinds():
    assert len(CASES) >= 10 and len({c.id for c in CASES}) == len(CASES)
    assert all(c.features <= set(FEATURES) for c in CASES)
    assert any(c.vague and c.features == frozenset() for c in CASES)             # vague
    assert any(not c.vague for c in CASES) and any(c.hostile for c in CASES)    # clear, injection
    assert any(len(c.text) < 10 for c in CASES)                                 # empty-ish
    assert all(c.vague == (not c.features) for c in CASES if not c.hostile)


# ------------------------------------------------------------------ scoring
@pytest.mark.parametrize("expected, got, want", [
    (frozenset({"a", "b"}), {"a", "b"}, (1.0, 1.0)),
    (frozenset({"a", "b"}), {"a"}, (1.0, 0.5)),
    (frozenset({"a"}), {"a", "x"}, (0.5, 1.0)),
    (frozenset({"a"}), {"x"}, (0.0, 0.0)),
    (frozenset(), set(), (1.0, 1.0)),
    (frozenset(), {"x"}, (0.0, 1.0)),
    (frozenset({"a"}), set(), (0.0, 0.0)),
])
def test_precision_recall(expected, got, want):
    assert prf(expected, got) == want


def test_requirements_scoring_rewards_the_right_features_and_the_right_vagueness_call():
    case = BY_ID["greenfield-clear"]
    good = run_checked(build_agent_registry(llm=ScriptedLLM([REQ_CLEAR]))["requirements_llm"], ctx(case.text),
                       ["requirements_complete"])
    score = score_requirements(case, good)
    assert (score["schema_valid"], score["gate_ok"], score["precision"], score["recall"], score["vague_correct"],
            score["policy_ok"]) == (True, True, 1.0, 1.0, True, True)

    flagged = run_checked(build_agent_registry(llm=ScriptedLLM([REQ_VAGUE]))["requirements_llm"], ctx(case.text),
                          ["requirements_complete"])
    wrong = score_requirements(case, flagged)               # a clear request flagged as vague, features missed
    assert wrong["vague_correct"] is False and wrong["recall"] == 0.0 and wrong["gate_ok"] is True

    vague = score_requirements(BY_ID["vague-safer"], flagged)
    assert vague["vague_correct"] is True and vague["precision"] == 1.0


def test_failures_are_scored_not_raised():
    case = BY_ID["greenfield-clear"]
    registry = build_agent_registry(llm=ScriptedLLM(["not json", LLMUnavailable("down"), {**REQ_CLEAR, "assumptions": [
        f"password={SECRET}"]}]))
    outcomes = [run_checked(registry["requirements_llm"], ctx(case.text), ["requirements_complete"]) for _ in range(3)]
    assert [o.status for o in outcomes] == ["invalid_output", "unavailable", "policy_violation"]
    scores = [score_requirements(case, o) for o in outcomes]
    assert [s["schema_valid"] for s in scores] == [False, True, True]
    assert [s["policy_ok"] for s in scores] == [True, True, False]
    assert not any(s["gate_ok"] or s["vague_correct"] for s in scores) and SECRET not in json.dumps(scores)


# ------------------------------------------------------------------- runner
def test_evaluate_compares_deterministic_and_llm_per_family():
    cases = [BY_ID["greenfield-clear"], BY_ID["vague-safer"], BY_ID["paraphrase-analytics"]]
    llm = ScriptedLLM([REQ_CLEAR, ARCH, NARRATIVE,           # greenfield-clear: requirements, architecture, docs
                       REQ_VAGUE, NORMALIZE,                 # vague-safer: requirements, normalize
                       {**REQ_CLEAR, "detected_features": ["analytics"]}, ARCH, NARRATIVE])
    rows = evaluate(cases, modes(llm))
    assert [(r["case"], r["mode"]) for r in rows] == [(c.id, m) for c in cases for m in ("deterministic", "llm")]
    assert len(llm.calls) == 8 and not llm.queue

    stats = summary(rows)
    assert set(stats) == set(FAMILIES)
    assert stats["requirements"]["llm"]["n"] == 3 and stats["requirements"]["llm"]["gate_ok"] == 1.0
    assert stats["requirements"]["llm"]["vague_accuracy"] == 1.0 and stats["requirements"]["llm"]["recall"] == 1.0
    assert stats["requirements"]["deterministic"]["recall"] < 1.0               # the paraphrase is missed offline
    assert stats["normalize"]["llm"]["gate_ok"] == 1.0 and stats["architecture"]["llm"]["n"] == 2
    assert all(stats[f]["llm"]["policy_violations"] == 0 for f in stats)
    text = format_report(rows)
    assert "deterministic" in text and "llm" in text and "Summary per agent family" in text


def test_injection_case_deterministic_echoes_it_but_a_well_behaved_model_does_not():
    case = BY_ID["injection"]
    assert SECRET in case.text and INJECTION                                  # a hostile text with a planted secret
    clean = {**REQ_CLEAR, "detected_features": ["alias"], "problem": "Callers want custom aliases for links."}
    rows = evaluate([case], modes(ScriptedLLM([clean, ARCH, NARRATIVE])))
    by_mode = {r["mode"]: r["families"]["requirements"] for r in rows}
    assert by_mode["deterministic"]["policy_ok"] is False        # it pastes the request text, secret included
    assert by_mode["llm"]["policy_ok"] is True and by_mode["llm"]["recall"] == 1.0


def test_a_dead_model_gives_zero_scores_without_crashing_the_run():
    rows = evaluate(CASES[:3], modes(ScriptedLLM([LLMUnavailable("no network")], repeat_last=True)))
    llm_rows = [r["families"] for r in rows if r["mode"] == "llm"]
    assert all(f["requirements"]["gate_ok"] is False and f["normalize"] is None for f in llm_rows)
    assert summary(rows)["requirements"]["llm"]["gate_ok"] == 0.0


# ----------------------------------------------------------------- throttle
def test_throttle_pauses_between_calls_and_retries_once_after_a_429():
    slept, fake = [], ScriptedLLM([{"x": 1}, LLMRateLimited("429", 7.0), {"x": 2}])

    from pydantic import BaseModel

    class Schema(BaseModel):
        x: int

    client = Throttled(fake, pause_s=100.0, sleep=slept.append)
    assert client.generate_json("s", "p", Schema).x == 1 and slept == []     # first call: no pause
    assert client.generate_json("s", "p", Schema).x == 2
    assert 90 < slept[0] <= 100 and slept[1] == 7.0                          # pause, then the Retry-After wait
    assert client.model == "scripted"


# ---------------------------------------------------------------------- cli
def test_cli_offline_json(capsys):
    assert main(["--cases", "2", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["case"] for r in data["cases"]] == ["greenfield-clear", "brownfield-clear"]
    assert set(data["summary"]["requirements"]) == {"deterministic"}


def test_cli_text_table(capsys):
    assert main(["--cases", "3"]) == 0
    assert "Summary per agent family" in capsys.readouterr().out


def test_cli_llm_without_a_key_exits_2(monkeypatch, capsys):
    monkeypatch.setattr("evals.cli.get_settings", lambda: LLMSettings(provider="none"))
    assert main(["--llm", "--cases", "1"]) == 2
    assert "not configured" in capsys.readouterr().err


def test_rate_limit_pauses_are_not_counted_as_model_latency():
    slept = []
    fake = ScriptedLLM([REQ_CLEAR, ARCH, NARRATIVE])
    client = Throttled(fake, pause_s=50.0, sleep=slept.append)      # records the pauses instead of sleeping
    rows = evaluate([BY_ID["greenfield-clear"]], {"llm": (build_agent_registry(llm=client), "_llm", client)})
    assert len(slept) == 2 and client.paused_s == sum(slept) > 90
    assert all(f["ms"] < 5000 for f in rows[0]["families"].values() if f)
