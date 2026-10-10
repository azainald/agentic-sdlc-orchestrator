"""Settings, the tiny .env parser, and how a scenario opts in to the LLM agents."""
import pytest

from orchestrator.llm.config import DEFAULT_MODEL, FALLBACK_MODELS, get_settings, read_env_file
from scenarios.common import llm_graph, resolve_llm, with_llm
from scenarios.registry import SCENARIOS, get_scenario, list_scenarios


def settings(tmp_path, env=None, file_text=None):
    path = tmp_path / ".env"
    if file_text is not None:
        path.write_text(file_text, encoding="utf-8")
    return get_settings(environ=env or {}, env_file=path)


# ------------------------------------------------------------------ .env
def test_env_parser_handles_comments_quotes_export_and_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text("﻿# comment\n\nA=1\nexport B = two \nC=\"quoted value\"\nD='single'\nE=plain # trailing\n"
                    "not a pair\nF=a=b\n", encoding="utf-8")
    assert read_env_file(path) == {"A": "1", "B": "two", "C": "quoted value", "D": "single", "E": "plain",
                                   "F": "a=b"}


def test_env_parser_missing_file_is_empty(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


# -------------------------------------------------------------- settings
def test_default_is_off_and_unconfigured(tmp_path):
    s = settings(tmp_path)
    assert (s.provider, s.configured, s.enabled, s.active) == ("none", False, False, False)
    assert s.model == DEFAULT_MODEL == "gemini-3.5-flash-lite"
    assert s.fallback_models == FALLBACK_MODELS == ("gemini-flash-lite-latest", "gemini-3.1-flash-lite")


def test_a_key_alone_is_configured_but_not_active(tmp_path):
    """Opt-in: the LLM is used only with LLM_ENABLED=1 or an explicit run parameter."""
    s = settings(tmp_path, file_text="GEMINI_API_KEY=abc\n")
    assert s.provider == "gemini" and s.configured and not s.enabled and not s.active


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_key_plus_enabled_flag_is_active(tmp_path, flag):
    assert settings(tmp_path, file_text=f"GEMINI_API_KEY=abc\nLLM_ENABLED={flag}\n").active


@pytest.mark.parametrize("flag", ["0", "false", "", "no", "maybe"])
def test_other_values_do_not_enable(tmp_path, flag):
    assert not settings(tmp_path, {"GEMINI_API_KEY": "abc", "LLM_ENABLED": flag}).active


def test_enabled_without_a_key_is_not_active(tmp_path):
    s = settings(tmp_path, {"LLM_ENABLED": "1"})
    assert s.enabled and not s.configured and not s.active


def test_real_environment_beats_the_env_file(tmp_path):
    s = settings(tmp_path, {"GEMINI_API_KEY": "from-env", "LLM_ENABLED": "0"},
                 file_text="GEMINI_API_KEY=from-file\nLLM_ENABLED=1\nLLM_MODEL=custom\n")
    assert s.api_key == "from-env" and not s.enabled and s.model == "custom"


def test_provider_none_and_unknown_providers_disable(tmp_path):
    assert not settings(tmp_path, {"GEMINI_API_KEY": "k", "LLM_ENABLED": "1", "LLM_PROVIDER": "none"}).configured
    assert settings(tmp_path, {"GEMINI_API_KEY": "k", "LLM_PROVIDER": "skynet"}).provider == "none"


def test_settings_repr_hides_the_key(tmp_path):
    assert "super-secret" not in repr(settings(tmp_path, {"GEMINI_API_KEY": "super-secret"}))


def test_get_settings_does_not_write_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings(tmp_path, file_text="GEMINI_API_KEY=abc\n")
    import os
    assert "GEMINI_API_KEY" not in os.environ


# ------------------------------------------------------------- scenarios
def test_resolve_llm_explicit_param_wins_otherwise_settings(monkeypatch):
    assert resolve_llm({"llm": True}) is True and resolve_llm({"llm": False}) is False
    assert resolve_llm({}) is False and resolve_llm(None) is False       # tests run with LLM_ENABLED=0
    monkeypatch.setenv("LLM_ENABLED", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert resolve_llm({}) is True and resolve_llm({"llm": False}) is False


@pytest.mark.parametrize("value", ["0", "false", "", "yes", 0, 1, [], {}])
def test_resolve_llm_rejects_anything_but_a_real_bool(value):
    """'0' / 'false' are truthy strings: they must not silently switch the LLM layer on."""
    with pytest.raises(ValueError, match="params.llm must be true or false"):
        resolve_llm({"llm": value})


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_default_scenario_is_exactly_the_offline_graph(name):
    offline = SCENARIOS[name]()
    default = get_scenario(name)
    assert default.graph == offline.graph and default.params == offline.params
    assert get_scenario(name, llm=False).graph == offline.graph
    assert not any(s.agent.endswith('_llm') for s in default.graph)
    assert [s.model_dump() for s in list_scenarios() if s.name == name] == [offline.model_dump()]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_llm_scenario_swaps_agents_and_keeps_the_deterministic_fallback(name):
    offline, llm = SCENARIOS[name](), get_scenario(name, llm=True)
    assert llm.params["llm"] is True and "llm" not in offline.params
    assert [s.id for s in llm.graph] == [s.id for s in offline.graph]
    swapped = {s.id: s for s in llm.graph if s.agent.endswith("_llm")}
    assert {"requirements", "architecture", "documentation"} <= set(swapped)
    for stage_id, spec in swapped.items():
        original = next(s for s in offline.graph if s.id == stage_id)
        assert spec.agent == f"{original.agent}_llm" and spec.fallback_agent == original.agent
        assert spec.max_attempts >= 2 and spec.timeout_ms and not spec.requires_approval
    for before, after in zip(offline.graph, llm.graph):  # everything else (gates, deps, approvals) is untouched
        assert after.model_copy(update={"agent": before.agent, "fallback_agent": before.fallback_agent,
                                        "max_attempts": before.max_attempts, "timeout_ms": before.timeout_ms}) == before


def test_impact_and_approval_stages_stay_deterministic():
    graph = {s.id: s for s in get_scenario("brownfield", llm=True).graph}
    assert graph["impact"].agent == "impact" and graph["api_change_review"].agent == "api_change_review"
    ambiguous = {s.id: s for s in get_scenario("ambiguous", llm=True).graph}
    assert ambiguous["apply_interpretation"].agent == "apply_interpretation"
    assert ambiguous["apply_interpretation"].fallback_agent is None and ambiguous["normalize"].agent == "normalize_llm"


def test_llm_graph_is_pure():
    scenario = SCENARIOS["greenfield"]()
    before = scenario.model_dump()
    with_llm(scenario)
    llm_graph(scenario.graph)
    assert scenario.model_dump() == before
