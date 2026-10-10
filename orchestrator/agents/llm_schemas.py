"""What an LLM agent may say: small, size-capped pydantic schemas.

The model's JSON is validated against these. Unknown keys are dropped (only declared fields are ever read),
so a model cannot smuggle extra fields such as ``approved: true`` into an artifact. The ``Literal`` types
tie the model to the product's real vocabulary (feature keys, question topics)."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints

from .features import FEATURES
from .requirements import QUESTIONS

FeatureKey = Literal[tuple(FEATURES)]      # alias, ttl, analytics, ...
Topic = Literal[tuple(QUESTIONS)]          # scope, accountability, privacy, ...

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Medium = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]


class OpenQuestion(BaseModel):
    topic: Topic
    question: Medium


class RequirementsOut(BaseModel):
    problem: Medium
    goals: list[Short] = Field(min_length=1, max_length=8)
    acceptance_criteria: list[Medium] = Field(min_length=1, max_length=12)
    non_goals: list[Short] = Field(default_factory=list, max_length=8)
    assumptions: list[Short] = Field(min_length=1, max_length=8)
    detected_features: list[FeatureKey] = Field(default_factory=list, max_length=len(FEATURES))
    open_questions: list[OpenQuestion] = Field(default_factory=list, max_length=6)
    vague_terms: list[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=8)


class Proposal(BaseModel):
    topic: Topic
    proposed_resolution: Medium
    rationale: Medium
    adds_feature: Optional[FeatureKey] = None


class NormalizeOut(BaseModel):
    proposals: list[Proposal] = Field(min_length=1, max_length=6)


class ComponentOut(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    module: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    responsibility: Medium


class DataChange(BaseModel):
    change: Medium
    feature: Annotated[str, StringConstraints(max_length=30)]


class ApiDelta(DataChange):
    public_contract: bool = False


class DecisionOut(BaseModel):
    title: Short
    context: Medium
    decision: Medium
    consequence: Medium
    feature: Annotated[str, StringConstraints(max_length=30)]


class ArchitectureOut(BaseModel):
    components: list[ComponentOut] = Field(min_length=1, max_length=12)
    data_model: list[DataChange] = Field(default_factory=list, max_length=12)
    api_deltas: list[ApiDelta] = Field(default_factory=list, max_length=12)
    decisions: list[DecisionOut] = Field(min_length=1, max_length=10)


class NarrativeOut(BaseModel):
    overview: Long
    highlights: list[Medium] = Field(min_length=1, max_length=6)
    caveats: list[Medium] = Field(default_factory=list, max_length=6)
