"""Optional LLM layer: a small client interface, a Gemini REST client, settings and a scripted fake.

LLM proposes; deterministic tools verify; gates and humans decide. See docs/LLM.md.
"""
from __future__ import annotations

from typing import Optional

from .client import LLMClient
from .config import LLMSettings, get_settings
from .errors import LLMError, LLMInvalidOutput, LLMRateLimited, LLMUnavailable
from .fake import ScriptedLLM

__all__ = ["LLMClient", "LLMError", "LLMInvalidOutput", "LLMRateLimited", "LLMSettings", "LLMUnavailable",
           "ScriptedLLM", "build_client", "get_settings"]


def build_client(settings: Optional[LLMSettings] = None) -> LLMClient:
    """The configured client, or ``LLMUnavailable`` (the engine then falls back to the deterministic agent)."""
    settings = settings or get_settings()
    if settings.provider == "gemini" and settings.api_key:
        from .gemini import GeminiClient
        return GeminiClient(settings.api_key, settings.model, settings.fallback_models)
    raise LLMUnavailable("LLM is not configured (set GEMINI_API_KEY in .env or the environment)")
