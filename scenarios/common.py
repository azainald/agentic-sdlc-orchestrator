"""Stages every scenario shares: parallel implementation join, verification, release."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from orchestrator.llm import get_settings
from orchestrator.models import Autonomy, StageSpec

from .base import Scenario

TESTING_TIMEOUT_MS = 120_000  # per attempt: the real pytest subprocess must not hang the run
LLM_TIMEOUT_MS = 120_000      # per attempt: a slow or rate-limited model must not hang the run
LLM_STAGES = {"requirements", "normalize", "architecture", "documentation"}  # agents that have a <name>_llm twin


def resolve_llm(params: Optional[Mapping[str, Any]] = None) -> bool:
    """Should this run use the LLM agents? An explicit ``params["llm"]`` wins; otherwise the settings decide
    (``LLM_ENABLED`` truthy and a key present). Without opt-in the answer is False: runs stay deterministic."""
    explicit = (params or {}).get("llm")
    return bool(explicit) if explicit is not None else get_settings().active


def llm_graph(graph: list[StageSpec]) -> list[StageSpec]:
    """The same graph with the LLM agent on every stage that has one. The deterministic agent becomes the
    fallback, so a missing key, an outage, a rate limit or garbage output degrades through the engine's
    retry -> fallback ladder (visible in the audit trail) instead of failing the run.

    Stages that need a human (the approval stages) are never touched: a human approved *that* agent."""
    out = []
    for spec in graph:
        if spec.agent in LLM_STAGES and not spec.requires_approval:
            spec = spec.model_copy(update={"agent": f"{spec.agent}_llm", "fallback_agent": spec.agent,
                                           "max_attempts": max(2, spec.max_attempts),
                                           "timeout_ms": spec.timeout_ms or LLM_TIMEOUT_MS})
        out.append(spec)
    return out


def with_llm(scenario: Scenario) -> Scenario:
    """The LLM variant of a scenario (``params["llm"] = True`` so reports can show it)."""
    return scenario.model_copy(update={"graph": llm_graph(scenario.graph), "params": {**scenario.params, "llm": True}})


def implementation_stages(parts: dict[str, str], after: str, consumes: list[str]) -> list[StageSpec]:
    """Parallel ``impl_*`` stages (``parts``: stage id -> title), all started by ``after``."""
    return [
        StageSpec(id=stage_id, title=title, agent=stage_id, depends_on=[after], parallel_group="impl",
                  entry_gates=["architecture_present"], consumes=consumes)
        for stage_id, title in parts.items()
    ]


def delivery_stages(impl_ids: list[str], docs_consumes: list[str]) -> list[StageSpec]:
    """sync (join all) -> testing || documentation -> release (human approval).

    Each ``impl_<x>`` stage publishes the artifact ``impl_<x>``, which ``sync`` consumes."""
    return [
        StageSpec(id="sync", title="Merge implementation parts", agent="sync", depends_on=impl_ids, join="all",
                  exit_gates=["implementation_complete"], max_attempts=1, consumes=list(impl_ids)),
        StageSpec(id="testing", title="Run the shortener test-suite and smoke test", agent="testing",
                  depends_on=["sync"], entry_gates=["implementation_complete"], exit_gates=["tests_green"],
                  max_attempts=3, fallback_agent="testing_fallback", rollback_agent="rollback_testing",
                  parallel_group="verify", consumes=["implementation"], timeout_ms=TESTING_TIMEOUT_MS),
        StageSpec(id="documentation", title="Generate documentation", agent="documentation",
                  depends_on=["sync"], entry_gates=["implementation_complete"], exit_gates=["docs_present"],
                  parallel_group="verify", consumes=["implementation", *docs_consumes]),
        StageSpec(id="release", title="Release go/no-go", agent="release", depends_on=["testing", "documentation"],
                  entry_gates=["release_ready", "docs_present", "no_policy_exceptions"],
                  requires_approval=True, approval_kind="production_release", autonomy=Autonomy.HUMAN_GATED,
                  max_attempts=1, rollback_agent="rollback_release",
                  consumes=["test_report", "documentation", "implementation"]),
    ]
