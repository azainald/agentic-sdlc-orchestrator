"""Ambiguous: a vague request that must be interpreted, and the interpretation approved, before design."""
from orchestrator.agents.requirements import REQUIREMENT_INTERPRETATION
from orchestrator.models import Autonomy, StageSpec

from .base import Scenario
from .common import delivery_stages, implementation_stages

REQUIREMENT = "Make links safer"

PARTS = {
    "impl_ssrf": "Verify the destination guard",
    "impl_audit": "Design audit logging",
    "impl_retention": "Design data retention",
}


def build() -> Scenario:
    graph = [
        StageSpec(id="requirements", title="Structure the requirement", agent="requirements",
                  exit_gates=["requirements_complete"]),
        StageSpec(id="normalize", title="Propose bounded interpretations", agent="normalize",
                  depends_on=["requirements"], entry_gates=["requirements_complete"], consumes=["requirements"]),
        # The human reviews normalized_requirements (the proposals); approval lets this stage apply them.
        StageSpec(id="apply_interpretation", title="Apply the approved interpretation", agent="apply_interpretation",
                  depends_on=["normalize"], exit_gates=["ambiguity_resolved"],
                  requires_approval=True, approval_kind=REQUIREMENT_INTERPRETATION, autonomy=Autonomy.HUMAN_GATED,
                  consumes=["normalized_requirements"]),
        StageSpec(id="architecture", title="Design components and decisions", agent="architecture",
                  depends_on=["apply_interpretation"], entry_gates=["requirements_complete", "ambiguity_resolved"],
                  exit_gates=["architecture_present"], consumes=["requirements", "normalized_requirements"]),
        *implementation_stages(PARTS, after="architecture", consumes=["architecture"]),
        *delivery_stages(list(PARTS), docs_consumes=["normalized_requirements", "architecture"]),
    ]
    return Scenario(name="ambiguous", title="Ambiguous: 'Make links safer'", requirement=REQUIREMENT, graph=graph,
                    description="The agents detect vagueness, propose bounded interpretations, and nothing proceeds "
                                "until a human approves them and the ambiguity gate passes.")
