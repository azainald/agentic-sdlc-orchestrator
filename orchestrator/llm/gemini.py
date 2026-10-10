"""Gemini over plain REST (httpx). No SDK.

The API key travels ONLY in the ``x-goog-api-key`` header, never in the URL, and is scrubbed from every
error message. 429 and 5xx get a small bounded retry that honours ``Retry-After``; a model that does not
exist for this key (404) or stays rate limited hands over to the next model in the list.
A refused key (401, 403, 400 "API key not valid") is permanent: no other model is tried, and the client then
fails every later call immediately, without a network request (a small circuit breaker).
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

import httpx

from .client import T, parse_model_json
from .config import DEFAULT_MODEL, FALLBACK_MODELS
from .errors import LLMAuthError, LLMInvalidOutput, LLMRateLimited, LLMUnavailable

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_RETRIES = 2                 # per model, on 429 / 5xx
MAX_RETRY_AFTER_S = 20.0        # a longer Retry-After is not worth blocking a stage for: give up on this model
DEADLINE_S = 100.0              # total time one generate_json call may spend over all models and retries


class GeminiClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, fallback_models: tuple[str, ...] = FALLBACK_MODELS,
                 *, timeout: float = 45.0, transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set")
        self._key = api_key
        self.models = [model, *(m for m in fallback_models if m != model)]
        self.model = model                      # the model that answered last
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._sleep = sleep
        self._auth_failure: Optional[str] = None  # set once the key is refused: the circuit is then open for good

    # ------------------------------------------------------------------ public
    def generate_json(self, system: str, prompt: str, schema: type[T], *, temperature: float = 0.2,
                      max_output_tokens: int = 2048) -> T:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
                # Thinking tokens count against maxOutputTokens and can leave the answer empty.
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        }
        if self._auth_failure:
            raise LLMAuthError(self._auth_failure)
        deadline = time.monotonic() + DEADLINE_S
        last: Exception = LLMUnavailable("no model configured")
        for model in self.models:
            try:
                text = self._answer(model, body, deadline)
                self.model = model
                return parse_model_json(text, schema)
            except LLMAuthError as exc:  # the key itself is refused: other models would be refused too
                self._auth_failure = f"{exc} (the API key was refused earlier; not calling the API again)"
                raise
            except LLMUnavailable as exc:  # missing model, rate limit, 5xx, block: the next model may still answer
                last = exc
            if time.monotonic() > deadline:
                break
        raise last

    # ----------------------------------------------------------------- private
    def _answer(self, model: str, body: dict[str, Any], deadline: float) -> str:
        """One model: POST with bounded retries, return the answer text."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self._http.post(f"{BASE_URL}/{model}:generateContent", json=body,
                                           headers={"x-goog-api-key": self._key})
            except httpx.TimeoutException:
                raise LLMUnavailable(f"{model}: request timed out") from None
            except httpx.HTTPError as exc:
                raise LLMUnavailable(f"{model}: network error ({type(exc).__name__})") from None
            status = response.status_code
            if status == 200:
                return self._extract(model, response)
            retry_after = _retry_after(response)
            if status == 404:
                raise LLMUnavailable(f"{model}: model not found for this key")
            if status != 429 and status < 500:
                detail = f"{model}: HTTP {status} {self._error_text(response)}"
                if status in (401, 403) or (status == 400 and "api key not valid" in detail.lower()):
                    raise LLMAuthError(detail)
                raise LLMUnavailable(detail)
            too_long = retry_after is not None and retry_after > MAX_RETRY_AFTER_S
            if attempt == MAX_RETRIES or too_long or time.monotonic() > deadline:
                detail = f"{model}: HTTP {status}" + (f", retry after {retry_after:.0f}s" if retry_after else "")
                if status == 429:
                    raise LLMRateLimited(detail, retry_after)
                raise LLMUnavailable(detail)
            self._sleep(retry_after if retry_after is not None else 1.0 * 2 ** attempt)
        raise LLMUnavailable(f"{model}: unreachable")  # pragma: no cover

    def _extract(self, model: str, response: httpx.Response) -> str:
        try:
            data = response.json()
            candidate = (data.get("candidates") or [None])[0]
        except (ValueError, AttributeError):
            raise LLMInvalidOutput(f"{model}: response is not JSON") from None
        if not candidate:
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMUnavailable(f"{model}: no candidates" + (f" (blocked: {block})" if block else ""))
        reason = candidate.get("finishReason", "")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise LLMUnavailable(f"{model}: answer blocked ({reason})")
        if reason == "MAX_TOKENS":
            raise LLMInvalidOutput(f"{model}: answer cut off by the token limit")
        if not text.strip():
            raise LLMInvalidOutput(f"{model}: empty answer (finishReason={reason or 'none'})")
        return text

    def _error_text(self, response: httpx.Response) -> str:
        try:
            message = str(response.json()["error"]["message"])
        except (ValueError, KeyError, TypeError):
            message = ""
        return message.replace(self._key, "<redacted>")[:160]


def _retry_after(response: httpx.Response) -> Optional[float]:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None
