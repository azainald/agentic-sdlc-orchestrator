"""Name -> scenario factory, shared by the CLI, the HTTP API and tests."""
from __future__ import annotations

from typing import Callable

from . import ambiguous, brownfield, greenfield
from .base import Scenario
from .common import resolve_llm, with_llm

SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "greenfield": greenfield.build,
    "brownfield": brownfield.build,
    "ambiguous": ambiguous.build,
}


def get_scenario(name: str, llm: bool | None = None) -> Scenario:
    """``llm=None`` follows the settings (opt-in: ``LLM_ENABLED=1`` plus a key); True/False force it."""
    try:
        build = SCENARIOS[name]
    except KeyError:
        raise ValueError(f"unknown scenario '{name}'; known: {sorted(SCENARIOS)}") from None
    scenario = build()
    return with_llm(scenario) if resolve_llm({"llm": llm}) else scenario


def list_scenarios() -> list[Scenario]:
    return [build() for build in SCENARIOS.values()]
