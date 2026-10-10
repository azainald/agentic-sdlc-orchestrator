"""Brownfield: extend the existing shortener; impact analysis and a public-API review come first."""
from orchestrator.models import Autonomy, StageSpec

from .base import Scenario
from .common import delivery_stages, implementation_stages

REQUIREMENT = (
    "Extend the existing URL shortener: let users choose a custom alias for a link, support link expiry (ttl), "
    "and rate limit link creation per client."
)

PARTS = {
    "impl_alias": "Verify custom aliases",
    "impl_ttl": "Verify link expiry",
    "impl_ratelimit": "Verify rate limiting",
}


def build() -> Scenario:
    graph = [
        StageSpec(id="requirements", title="Structure the requirement", agent="requirements",
                  exit_gates=["requirements_complete"]),
        StageSpec(id="impact", title="Map impact on the existing code", agent="impact", depends_on=["requirements"],
                  entry_gates=["requirements_complete"], exit_gates=["impact_mapped"], consumes=["requirements"]),
        StageSpec(id="architecture", title="Design the change", agent="architecture", depends_on=["impact"],
                  entry_gates=["impact_mapped", "ambiguity_resolved"], exit_gates=["architecture_present"],
                  consumes=["requirements", "impact"]),
        StageSpec(id="api_change_review", title="Review public API changes", agent="api_change_review",
                  depends_on=["architecture"], entry_gates=["architecture_present"],
                  requires_approval=True, approval_kind="public_api_change", autonomy=Autonomy.HUMAN_GATED,
                  consumes=["impact", "architecture"]),
        *implementation_stages(PARTS, after="api_change_review", consumes=["architecture", "impact"]),
        *delivery_stages(list(PARTS), docs_consumes=["requirements", "architecture", "impact"]),
    ]
    return Scenario(name="brownfield", title="Brownfield: extend the existing shortener", requirement=REQUIREMENT,
                    graph=graph,
                    description="Impact analysis over the real source (ast), human review of public API changes, "
                                "then parallel verification of alias, ttl and rate limiting.")
