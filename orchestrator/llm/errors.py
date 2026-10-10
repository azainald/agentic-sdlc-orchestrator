"""Errors of the LLM layer. Every one is an ordinary ``Exception``, so the engine treats a failing model
call as a failed attempt (retry, then the deterministic fallback agent) and never as an engine crash."""
from __future__ import annotations


class LLMError(Exception):
    """Base class. Messages are short and never contain credentials or full model output."""


class LLMUnavailable(LLMError):
    """No model could answer: not configured, network down, timeout, 4xx/5xx, blocked or truncated answer."""


class LLMRateLimited(LLMUnavailable):
    """The provider answered 429 and the bounded internal retries did not help."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMInvalidOutput(LLMError):
    """The model answered, but not with JSON that fits the requested schema."""
