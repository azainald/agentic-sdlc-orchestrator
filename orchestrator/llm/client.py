"""The one interface agents depend on, and the shared JSON validation behind it."""
from __future__ import annotations

import json
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import LLMInvalidOutput

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


class LLMClient(Protocol):
    """``generate_json`` returns an instance of ``schema`` or raises an ``LLMError``. Nothing else.

    The model gets text in and gives JSON out: no tools, no access to run state, approvals or routing."""

    model: str  # the model that answered last (provenance for artifacts)

    def generate_json(self, system: str, prompt: str, schema: type[T], *, temperature: float = 0.2,
                      max_output_tokens: int = 2048) -> T: ...


def strip_fences(text: str) -> str:
    """Models often wrap JSON in ```json ... ``` even when told not to."""
    match = _FENCE.match(text)
    return (match.group(1) if match else text).strip()


def parse_model_json(text: str, schema: type[T]) -> T:
    """Text -> validated ``schema`` instance. Error messages are short and never quote the model's output
    (it may hold anything, including text an attacker asked for)."""
    try:
        data = json.loads(strip_fences(text))
    except ValueError:
        raise LLMInvalidOutput("model output is not valid JSON") from None
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or '<root>'}: {e['msg']}" for e in exc.errors()[:3])
        raise LLMInvalidOutput(f"output does not match {schema.__name__}: {problems}"[:300]) from None
