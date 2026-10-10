"""Model output is untrusted text: control characters are rejected, and a model cannot switch off ambiguity
detection. Everything runs against a ScriptedLLM (no network)."""
import pytest
from pydantic import ValidationError

from orchestrator.agents import AGENTS
from orchestrator.agents.llm_schemas import NarrativeOut
from orchestrator.llm import LLMInvalidOutput, ScriptedLLM
from orchestrator.models import RunState, StageState

from .helpers import ARCH, NARRATIVE, NORMALIZE, REQ_CLEAR, REQ_VAGUE, make_engine, run_scenario
from .test_agents import agents, ctx, only
from scenarios.registry import get_scenario

ESCAPE_TEXT = "\x1b[2K\r fake line"   # erases the line, returns the cursor, then writes a forged one


# ------------------------------------------------------- control characters
@pytest.mark.parametrize("bad", [
    {**REQ_CLEAR, "problem": ESCAPE_TEXT},
    {**REQ_CLEAR, "goals": ["fine", "bell\x07"]},
    {**REQ_CLEAR, "acceptance_criteria": ["two\nlines"]},        # single-line fields: no newline either
    {**REQ_CLEAR, "assumptions": ["nul\x00"]},
    {**REQ_CLEAR, "assumptions": ["c1 csi \x9b2K"]},
    {**REQ_CLEAR, "vague_terms": ["ok\x1b[31m"]},
    {**REQ_VAGUE, "open_questions": [{"topic": "scope", "question": ESCAPE_TEXT}]},
])
def test_requirements_llm_rejects_control_characters_anywhere(bad):
    llm, _ = agents(bad)
    with pytest.raises(LLMInvalidOutput, match="control character"):
        llm["requirements_llm"](ctx())


def test_the_other_llm_agents_reject_control_characters_too():
    requirements = only(AGENTS["requirements"](ctx("Make links safer")))
    llm, _ = agents({"proposals": [{**p, "rationale": ESCAPE_TEXT} for p in NORMALIZE["proposals"]]})
    with pytest.raises(LLMInvalidOutput, match="control character"):
        llm["normalize_llm"](ctx("Make links safer", {"requirements": requirements}))
    arch_inputs = {"requirements": only(AGENTS["requirements"](ctx()))}
    for broken in ({**ARCH, "components": [{"name": "esc\x1b", "module": "m", "responsibility": "r"}]},
                   {**ARCH, "decisions": [{**ARCH["decisions"][0], "consequence": ESCAPE_TEXT}]}):
        llm, _ = agents(broken)
        with pytest.raises(LLMInvalidOutput, match="control character"):
            llm["architecture_llm"](ctx(artifacts=arch_inputs))
    llm, _ = agents({**NARRATIVE, "overview": ESCAPE_TEXT})
    with pytest.raises(LLMInvalidOutput, match="control character"):
        llm["documentation_llm"](ctx(artifacts={"requirements": {}, "implementation": {"parts": {}}}))


def test_only_the_narrative_may_use_newlines_and_tabs_and_never_carriage_returns():
    ok = NarrativeOut(**{**NARRATIVE, "overview": "Line one.\n\tLine two."})
    assert ok.overview == "Line one.\n\tLine two."
    with pytest.raises(ValidationError):
        NarrativeOut(**{**NARRATIVE, "overview": "Line one.\r\nLine two."})   # CR overwrites a terminal line


async def test_a_model_that_answers_with_escape_sequences_degrades_to_the_deterministic_fallback():
    """Rejected output is an ordinary failed attempt: retry, then the deterministic agent. Nothing the model wrote
    reaches an artifact."""
    hostile = {**REQ_CLEAR, "problem": ESCAPE_TEXT}
    llm = ScriptedLLM([hostile], repeat_last=True)
    run = await run_scenario("greenfield", llm)
    assert run.state is RunState.SUCCEEDED and run.stages["requirements"].used_fallback
    assert run.artifacts["requirements"].content["generator"] == "deterministic"
    assert "\x1b" not in str(run.artifacts["requirements"].content)
    assert len(llm.calls) >= 2 and any(d.kind == "retry" and "control character" in d.rationale for d in run.decisions)


# ------------------------------------------------------ ambiguity detection
def test_the_model_cannot_switch_off_ambiguity_detection():
    """'Make links safer' is vague by the deterministic check: a model that guesses a feature and raises no open
    question is invalid (engine: retry, then the deterministic fallback), never trusted."""
    guessed = {**REQ_CLEAR, "detected_features": ["ssrf"], "open_questions": []}
    for text in ("Make links safer", "Improve the shortener and make it more robust"):
        llm, _ = agents(guessed)
        with pytest.raises(LLMInvalidOutput, match="vague by the deterministic check"):
            llm["requirements_llm"](ctx(text))


def test_a_model_may_still_recognise_a_paraphrase_the_keyword_check_misses():
    text = "Let owners see how many times each link was opened and which pages sent people there."
    llm, _ = agents({**REQ_CLEAR, "detected_features": ["analytics"]})
    assert only(llm["requirements_llm"](ctx(text)))["detected_features"] == ["analytics"]


async def test_a_model_that_calls_a_vague_request_clear_cannot_skip_the_human_interpretation_approval():
    llm = ScriptedLLM([{**REQ_CLEAR, "detected_features": ["ssrf"]}], repeat_last=True)
    engine = make_engine(llm, auto_approve=False)
    scenario = get_scenario("ambiguous", llm=True)
    run = engine.create_run(scenario.name, "Make links safer", scenario.graph, {**scenario.params, "skip_pytest": True})
    run = await engine.run(run.id)
    assert run.state is RunState.WAITING_APPROVAL                          # the human still has to interpret it
    assert run.stages["requirements"].used_fallback and run.stages["apply_interpretation"].state is StageState.WAITING_APPROVAL
    assert run.artifacts["requirements"].content["open_questions"]


# ------------------------------------------------------------ a refused key
async def test_a_refused_key_costs_one_request_for_the_whole_run_not_six_per_stage():
    import httpx

    from orchestrator.llm.gemini import GeminiClient

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}})

    client = GeminiClient("AIzaSy-FAKE-KEY-0123456789abcdef", transport=httpx.MockTransport(handler))
    run = await run_scenario("greenfield", client)
    assert run.state is RunState.SUCCEEDED and run.metrics.fallbacks == 3        # all three LLM stages fell back
    assert len(calls) == 1                                                       # ... after ONE refused request
    assert all("refused" in d.rationale or "API key" in d.rationale for d in run.decisions if d.kind == "retry" and d.outcome == "failed")


# ----------------------------------------- amendment and the requirement text
async def test_an_amendment_reruns_documentation_even_when_the_model_returns_the_same_requirements():
    """An LLM may describe two different wordings with the identical 'problem': the requirements artifact (and so
    everything that consumes only artifacts) does not change. The changelog quotes the requirement TEXT, so the
    documentation stage must still run again, or the shipped changelog would show the old request."""
    from orchestrator.agents.common import PROJECT_ROOT
    from orchestrator.agents.documentation import run_docs_dir

    old, new = "Add expiring links and click analytics, rate limited.", "Add link expiry, analytics and rate limiting."
    llm = ScriptedLLM([REQ_CLEAR, ARCH, NARRATIVE,       # first pass: requirements, architecture, documentation
                       REQ_CLEAR, NARRATIVE])            # after the amendment: requirements again, documentation again
    engine = make_engine(llm, auto_approve=False)
    scenario = get_scenario("greenfield", llm=True)
    run = engine.create_run(scenario.name, old, scenario.graph, {**scenario.params, "skip_pytest": True})
    run = await engine.run(run.id)
    assert run.state is RunState.WAITING_APPROVAL
    changelog = run_docs_dir(PROJECT_ROOT, run.id) / "CHANGELOG.md"
    assert old in changelog.read_text(encoding="utf-8")
    before = run.artifacts["requirements"].sha256

    run = await engine.amend_requirement(run.id, new, "alice")

    assert run.artifacts["requirements"].sha256 == before                       # the model's answer did not change ...
    assert run.stages["architecture"].runs == 1                                  # ... so nothing else was re-derived
    assert run.stages["documentation"].runs == 2                                 # ... except the text-reading stage
    text = changelog.read_text(encoding="utf-8")
    assert new in text and old not in text
    assert [d.stage_id for d in run.decisions if d.kind == "replan" and d.outcome == "requirement_amended"] == [None]
    assert "documentation" in next(d for d in run.decisions if d.outcome == "requirement_amended").inputs["requeued"]
