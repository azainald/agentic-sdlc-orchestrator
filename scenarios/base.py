"""Shared shape of a scenario, so the CLI, the HTTP API and tests all treat
greenfield / brownfield / ambiguous runs the same way."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from orchestrator.models import StageSpec


class Scenario(BaseModel):
    name: str                                   # "greenfield" | "brownfield" | "ambiguous"
    title: str
    requirement: str                            # the raw, human-written requirement
    graph: list[StageSpec]                      # the explicit dependency graph
    params: dict[str, Any] = Field(default_factory=dict)  # default run params
    description: str = ""                       # what this scenario demonstrates
