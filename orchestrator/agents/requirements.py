"""Requirements agents: turn free text into a structured, testable spec, and
resolve ambiguity in a bounded, human-approved way.

    requirements          text            -> requirements (may carry open_questions)
    normalize             requirements    -> normalized_requirements (proposed answers)
    apply_interpretation  (after approval) -> normalized_requirements (answers applied)
"""
from __future__ import annotations

from orchestrator.models import AgentContext, AgentResult

from .features import FEATURES, SECURITY_VAGUE, detect_features, vague_terms

REQUIREMENT_INTERPRETATION = "requirement_interpretation"  # approval_kind used by the ambiguity branch

# topic -> (question, proposed resolution, rationale, feature added by that resolution)
QUESTIONS = {
    "scope": (
        "Which kind of harm should the change protect against: unsafe destinations (SSRF, dangerous schemes), "
        "abuse of link creation, or visitor privacy?",
        "Destination safety first: harden the SSRF and scheme guard on create and update. "
        "Abuse limits already exist (rate limiting) and are out of scope.",
        "A shortener's largest exposure is redirecting people to unsafe or internal targets.",
        "ssrf",
    ),
    "accountability": (
        "Must changes to links be traceable to an actor (audit trail)?",
        "Yes: an append-only audit entry for create, update and delete, with a hashed actor, never a raw key or IP. "
        "Design only in this iteration.",
        "Safety incidents cannot be investigated without knowing who changed what.",
        "audit",
    ),
    "privacy": (
        "How long may click data (hashed visitor, referrer) be kept?",
        "Purge clicks and deleted links older than 90 days. No change to the public API.",
        "Click data is personal-data adjacent; a bounded window limits exposure.",
        "retention",
    ),
    "compatibility": (
        "May existing API behaviour or response shapes change?",
        "No: the change must be backward compatible and add no new endpoints.",
        "Existing clients must keep working without a coordinated release.",
        None,
    ),
    "behaviour": (
        "Which concrete behaviour of the shortener should change?",
        "Nothing is changed until a stakeholder names a behaviour; this iteration only records the question.",
        "Without a named behaviour no acceptance test can be written.",
        None,
    ),
    "success": (
        "How will we know the change worked (what would a test observe)?",
        "Success is defined by the Given/When/Then criteria generated for the resolved features.",
        "A requirement without an observable outcome cannot be verified.",
        None,
    ),
}

BASE_NON_GOALS = ("A web UI", "Multi-tenant accounts or billing")
BASE_ASSUMPTIONS = (
    "Single-process deployment with the in-memory or JSON-file store",
    "Destination URLs are limited to http and https",
)


def _open_questions(text: str, features: list[str]) -> list[str]:
    """Topics that must be answered before design can start. Empty for a concrete request."""
    if features:
        return []
    if SECURITY_VAGUE.search(text):
        return ["scope", "accountability", "privacy", "compatibility"]
    return ["behaviour", "success"]


def is_explicitly_vague(text: str) -> bool:
    """The deterministic verdict that a request is ambiguous: it names a quality ('safer', 'robust', 'improve') and
    no concrete feature. Used to cross-check a model, which may not claim such a request is clear. (A text with no
    vague word and no keyword, e.g. a paraphrase, is NOT covered: recognising those is what the model is for.)"""
    return not detect_features(text) and bool(vague_terms(text))


def requirements_agent(ctx: AgentContext) -> AgentResult:
    text = ctx.requirement.strip()
    features = detect_features(text)
    topics = _open_questions(text, features)
    chosen = [FEATURES[k] for k in features]

    if chosen:
        problem = f'Stakeholders asked: "{text}". This implies: {", ".join(f.label for f in chosen)}.'
        goals = [f.goal for f in chosen]
        criteria = [c for f in chosen for c in f.criteria]
        criteria.append("Given any rejected request, When the API answers, "
                        "Then the body is {error: {code, message}} with a 4xx status")
    else:
        problem = (f'Stakeholders asked: "{text}". No concrete capability could be identified, '
                   f"so the intent is ambiguous (vague terms: {', '.join(vague_terms(text)) or 'none'}).")
        goals = ["Agree what the request means before any design or code is written"]
        criteria = ["Given the open questions, When stakeholders answer them, "
                    "Then every question has a recorded resolution before design starts"]

    content = {
        "problem": problem,
        "goals": goals,
        "acceptance_criteria": criteria,
        "non_goals": list(dict.fromkeys([*BASE_NON_GOALS, *(g for f in chosen for g in f.non_goals)])),
        "assumptions": list(dict.fromkeys([*BASE_ASSUMPTIONS, *(a for f in chosen for a in f.assumptions)])),
        "open_questions": [{"topic": t, "question": QUESTIONS[t][0], "resolution": ""} for t in topics],
        "detected_features": features,
        "ambiguity": {"is_ambiguous": bool(topics), "vague_terms": vague_terms(text)},
        "generator": "deterministic",
    }
    return AgentResult(artifacts={"requirements": content},
                       summary=f"{len(features)} feature(s) detected, {len(topics)} open question(s)")


def normalize_agent(ctx: AgentContext) -> AgentResult:
    """Propose one bounded answer per open question. Nothing is resolved yet:
    the gate keeps failing until a human approves the interpretation."""
    source = ctx.artifacts["requirements"]
    questions = []
    for q in source["open_questions"]:
        _, proposal, rationale, adds = QUESTIONS[q["topic"]]
        questions.append({**q, "resolution": "", "status": "proposed", "proposed_resolution": proposal,
                          "rationale": rationale, "adds_feature": adds})
    content = {**source, "open_questions": questions, "interpretation_status": "proposed",
               "generator": "deterministic"}
    return AgentResult(artifacts={"normalized_requirements": content},
                       summary=f"proposed interpretations for {len(questions)} question(s); awaiting approval")


def apply_interpretation_agent(ctx: AgentContext) -> AgentResult:
    """Runs only after a human approved the interpretation: write the answers in."""
    source = ctx.artifacts["normalized_requirements"]
    questions = [{**q, "resolution": q["proposed_resolution"], "status": "resolved"}
                 for q in source["open_questions"]]
    added = [q["adds_feature"] for q in questions if q.get("adds_feature")]
    features = list(dict.fromkeys([*source["detected_features"], *added]))
    new = [FEATURES[k] for k in features if k not in source["detected_features"]]
    content = {
        **source,
        "open_questions": questions,
        "detected_features": features,
        "interpretation_status": "resolved",
        "goals": [*source["goals"], *(f.goal for f in new)],
        "acceptance_criteria": [*source["acceptance_criteria"], *(c for f in new for c in f.criteria)],
        "assumptions": [*source["assumptions"], "Interpretation of the ambiguous request was approved by a human"],
    }
    return AgentResult(artifacts={"normalized_requirements": content},
                       summary=f"applied {len(questions)} approved resolution(s); features: {features}")
