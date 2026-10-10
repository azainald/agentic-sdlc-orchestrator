"""Stages every scenario shares: parallel implementation join, verification, release."""
from __future__ import annotations

from orchestrator.models import Autonomy, StageSpec


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
                  parallel_group="verify", consumes=["implementation"]),
        StageSpec(id="documentation", title="Generate documentation", agent="documentation",
                  depends_on=["sync"], entry_gates=["implementation_complete"], exit_gates=["docs_present"],
                  parallel_group="verify", consumes=["implementation", *docs_consumes]),
        StageSpec(id="release", title="Release go/no-go", agent="release", depends_on=["testing", "documentation"],
                  entry_gates=["release_ready", "docs_present", "no_policy_exceptions"],
                  requires_approval=True, approval_kind="production_release", autonomy=Autonomy.HUMAN_GATED,
                  max_attempts=1, rollback_agent="rollback_release",
                  consumes=["test_report", "documentation", "implementation"]),
    ]
