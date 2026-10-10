"""SDLC agents: deterministic by default (no network, no LLM, no API key), plus optional LLM-backed ones.

Every agent is a plain function ``AgentContext -> AgentResult`` that derives its
artifacts from the requirement text and from the real shortener code. The
registry maps the agent names used by ``StageSpec.agent`` to those functions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from orchestrator.llm import LLMClient
from orchestrator.models import AgentContext, AgentFn, AgentResult

from .architecture import architecture_agent
from .documentation import documentation_agent
from .impact import api_change_review_agent, impact_agent
from .implementation import IMPL_AGENTS
from .llm_agents import make_llm_agents
from .release import release_agent, rollback_release_agent
from .requirements import apply_interpretation_agent, normalize_agent, requirements_agent
from .testing import rollback_testing_agent, testing_agent, testing_fallback_agent

AGENTS: dict[str, AgentFn] = {
    "requirements": requirements_agent,
    "normalize": normalize_agent,
    "apply_interpretation": apply_interpretation_agent,
    "impact": impact_agent,
    "architecture": architecture_agent,
    "api_change_review": api_change_review_agent,
    **IMPL_AGENTS,
    "testing": testing_agent,
    "testing_fallback": testing_fallback_agent,
    "rollback_testing": rollback_testing_agent,
    "documentation": documentation_agent,
    "release": release_agent,
    "rollback_release": rollback_release_agent,
}


def _with_default_root(agent: AgentFn, root: Path) -> AgentFn:
    """Make ``root`` the shortener_root unless the run's params already name one."""
    def run(ctx: AgentContext) -> AgentResult:
        if "shortener_root" not in ctx.params:
            ctx = ctx.model_copy(update={"params": {**ctx.params, "shortener_root": str(root)}})
        return agent(ctx)
    run.__name__ = getattr(agent, "__name__", "agent")
    return run


def build_agent_registry(shortener_root: Optional[str | Path] = None, llm: Optional[LLMClient] = None) -> dict[str, AgentFn]:
    """name -> agent. ``shortener_root`` (default: this repository) is where agents find
    ``shortener/`` and ``tests/shortener/`` and where docs are written.

    The ``<name>_llm`` agents are always registered. They use ``llm`` (tests pass a ``ScriptedLLM``) or else the
    client built from the settings; when none is configured they raise ``LLMUnavailable`` and the engine
    falls back to the deterministic agent named in the stage."""
    agents = {**AGENTS, **make_llm_agents((lambda: llm) if llm is not None else None)}
    if shortener_root is None:
        return agents
    root = Path(shortener_root)
    return {name: _with_default_root(fn, root) for name, fn in agents.items()}
