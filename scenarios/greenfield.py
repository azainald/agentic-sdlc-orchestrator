"""Greenfield: a clear, new-product requirement."""
from orchestrator.models import StageSpec

from .base import Scenario
from .common import delivery_stages, implementation_stages

REQUIREMENT = (
    "Build a URL shortener service. Users create short links through a REST API, optionally with an expiry "
    "time (ttl). Visiting a short link redirects to the destination and records the click. Owners can read "
    "click analytics: total clicks, clicks per day, top referrers and unique visitors. Link creation is rate limited."
)

PARTS = {
    "impl_api": "Verify the HTTP API",
    "impl_store": "Verify the storage layer",
    "impl_analytics": "Verify click analytics",
}


def build() -> Scenario:
    graph = [
        StageSpec(id="requirements", title="Structure the requirement", agent="requirements",
                  exit_gates=["requirements_complete"]),
        StageSpec(id="architecture", title="Design components and decisions", agent="architecture",
                  depends_on=["requirements"], entry_gates=["requirements_complete", "ambiguity_resolved"],
                  exit_gates=["architecture_present"], consumes=["requirements"]),
        *implementation_stages(PARTS, after="architecture", consumes=["architecture"]),
        *delivery_stages(list(PARTS), docs_consumes=["requirements", "architecture"]),
    ]
    return Scenario(name="greenfield", title="Greenfield: new URL shortener", requirement=REQUIREMENT, graph=graph,
                    description="Clear requirement, no existing-code analysis: requirements, architecture, three "
                                "parallel implementation checks, sync, testing and docs in parallel, approved release.")
