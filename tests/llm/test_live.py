"""Tests against the real model. Skipped unless RUN_LIVE_LLM=1 and a key is configured (.env or environment).

    RUN_LIVE_LLM=1 python -m pytest tests/llm/test_live.py -q
"""
import os

import pytest

from orchestrator.agents import build_agent_registry
from orchestrator.llm import build_client, get_settings
from orchestrator.models import AgentContext, RunState
from scenarios.registry import get_scenario

from .helpers import make_engine

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM") != "1" or not get_settings().configured,
                       reason="set RUN_LIVE_LLM=1 and configure GEMINI_API_KEY to call the real model"),
]


def ctx(text: str) -> AgentContext:
    return AgentContext(run_id="live", stage_id="requirements", scenario="live", requirement=text, attempt=1)


def test_the_real_model_flags_a_vague_request_and_not_a_clear_one():
    agent = build_agent_registry(llm=build_client())["requirements_llm"]
    vague = agent(ctx("Make links safer")).artifacts["requirements"]
    assert vague["open_questions"] and all(q["resolution"] == "" for q in vague["open_questions"])
    clear = agent(ctx("Links must expire after a ttl, and owners can read click analytics.")).artifacts["requirements"]
    assert not clear["open_questions"] and {"ttl", "analytics"} <= set(clear["detected_features"])


async def test_a_whole_greenfield_run_with_the_real_model():
    engine = make_engine(None)
    engine.agents.update(build_agent_registry(llm=build_client()))
    scenario = get_scenario("greenfield", llm=True)
    run = engine.create_run(scenario.name, scenario.requirement, scenario.graph, {**scenario.params, "skip_pytest": True})
    run = await engine.run(run.id)
    assert run.state is RunState.SUCCEEDED
    assert run.artifacts["requirements"].content["generator"].startswith(("llm:", "deterministic"))
