"""Architecture agent: components, data model, API deltas and decisions (ADR style),
derived from the detected features and, for brownfield work, from the impact analysis."""
from __future__ import annotations

from orchestrator.models import AgentContext, AgentResult
from shortener.manifest import MANIFEST

from .common import effective_requirements
from .features import FEATURES

# The layered core every feature builds on: (name, module, responsibility).
CORE_COMPONENTS = [
    ("api", "app", "FastAPI routes: translate HTTP to service calls and errors to {error: {code, message}}"),
    ("service", "service", "Business logic: create (idempotent), redirect, update, delete, analytics"),
    ("store", "store", "UrlRepository interface with an in-memory / JSON-file implementation"),
    ("validation", "validation", "Destination, alias and ttl rules"),
]

CORE_DECISION = ("Layered app: routes -> service -> repository",
                 "The product must stay testable without a network or a database",
                 "Routes hold no logic, the service depends on the UrlRepository protocol, and the clock is injected",
                 "Swapping the store or faking time needs no change to the service")


def _adr(number: int, decision: tuple[str, str, str, str], feature: str) -> dict:
    title, context, chosen, consequence = decision
    return {"id": f"ADR-{number:03d}", "title": title, "context": context, "decision": chosen,
            "consequence": consequence, "feature": feature}


def architecture_agent(ctx: AgentContext) -> AgentResult:
    keys = effective_requirements(ctx).get("detected_features", [])
    chosen = [FEATURES[k] for k in keys]
    impact = ctx.artifacts.get("impact", {})
    already_there = {e["feature"] for e in impact.get("existing_support", []) if e["implemented"]}

    components = [{"name": n, "module": m, "responsibility": r, "new": False} for n, m, r in CORE_COMPONENTS]
    components += [{"name": f.component[0], "module": f.component[1], "responsibility": f.component[2],
                    "new": f.key not in already_there} for f in chosen]

    decisions = [_adr(1, CORE_DECISION, "core")]
    decisions += [_adr(i, f.decision, f.key) for i, f in enumerate(chosen, start=2)]

    content = {
        "features": keys,
        "components": components,
        "data_model": [{"change": c, "feature": f.key} for f in chosen for c in f.data_model],
        "api_deltas": [{"change": d, "feature": f.key, "public_contract": f.public_api}
                       for f in chosen for d in f.api_deltas],
        "decisions": decisions,
        "constraints": MANIFEST["limits"]["known_limitations"],
        "based_on": {"requirements": "normalized_requirements" if "normalized_requirements" in ctx.artifacts
                     else "requirements", "impact": bool(impact)},
        "generator": "deterministic",
    }
    return AgentResult(artifacts={"architecture": content},
                       summary=f"{len(components)} components, {len(decisions)} decisions for {keys}")
