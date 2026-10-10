"""Name -> scenario factory, shared by the CLI, the HTTP API and tests."""
from __future__ import annotations

from typing import Callable

from . import ambiguous, brownfield, greenfield
from .base import Scenario

SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "greenfield": greenfield.build,
    "brownfield": brownfield.build,
    "ambiguous": ambiguous.build,
}


def get_scenario(name: str) -> Scenario:
    try:
        build = SCENARIOS[name]
    except KeyError:
        raise ValueError(f"unknown scenario '{name}'; known: {sorted(SCENARIOS)}") from None
    return build()


def list_scenarios() -> list[Scenario]:
    return [build() for build in SCENARIOS.values()]
