"""A scripted stand-in for a model, for tests and offline demos. No network, fully deterministic."""
from __future__ import annotations

import json
from typing import Any, Union

from pydantic import BaseModel

from .client import T, parse_model_json
from .errors import LLMUnavailable

Response = Union[dict, str, BaseModel, Exception]


class ScriptedLLM:
    """Answers from a queue: a dict/str is validated against the requested schema exactly like real model
    output (so a wrong shape raises ``LLMInvalidOutput``), an exception is raised. Every call is recorded.

    ``repeat_last=True`` keeps answering with the final response once the queue is empty."""

    def __init__(self, responses: list[Response], *, repeat_last: bool = False, model: str = "scripted"):
        self.queue = list(responses)
        self.repeat_last = repeat_last
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, system: str, prompt: str, schema: type[T], *, temperature: float = 0.2,
                      max_output_tokens: int = 2048) -> T:
        self.calls.append({"system": system, "prompt": prompt, "schema": schema.__name__,
                           "temperature": temperature, "max_output_tokens": max_output_tokens})
        if not self.queue:
            raise LLMUnavailable("scripted LLM has no response left")
        response = self.queue[0] if self.repeat_last and len(self.queue) == 1 else self.queue.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, BaseModel):
            response = response.model_dump()
        return parse_model_json(response if isinstance(response, str) else json.dumps(response), schema)
