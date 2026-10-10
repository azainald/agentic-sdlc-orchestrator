"""LLM settings: real environment variables, then a git-ignored ``.env`` in the repo root.

The LLM layer is OPT-IN. Without ``LLM_ENABLED=1`` (or an explicit ``llm: true`` run parameter) every run
is fully deterministic and needs no key, so tests, CI and reviewers work offline.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = ("gemini-flash-lite-latest", "gemini-3.1-flash-lite")
_TRUTHY = {"1", "true", "yes", "on"}


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """A tiny ``.env`` parser: ``KEY=value`` lines, ``#`` comments, optional ``export``, optional quotes."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # inline comment after an unquoted value
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class LLMSettings:
    provider: str                      # "none" | "gemini"
    api_key: Optional[str] = None
    model: str = DEFAULT_MODEL
    fallback_models: tuple[str, ...] = FALLBACK_MODELS
    enabled: bool = False              # LLM_ENABLED is truthy

    @property
    def configured(self) -> bool:
        """A provider and its key are present (the LLM *can* be used)."""
        return self.provider != "none" and bool(self.api_key)

    @property
    def active(self) -> bool:
        """Opted in AND configured: scenarios use the LLM agents by default."""
        return self.enabled and self.configured

    def __repr__(self) -> str:  # never show the key, not even by accident in a log or traceback
        return (f"LLMSettings(provider={self.provider!r}, api_key={'<set>' if self.api_key else None}, "
                f"model={self.model!r}, enabled={self.enabled})")


def get_settings(environ: Optional[Mapping[str, str]] = None, env_file: Path = ENV_FILE) -> LLMSettings:
    """Real environment variables win over ``.env``. Nothing is written to ``os.environ``."""
    env: Mapping[str, Any] = {**read_env_file(env_file), **(os.environ if environ is None else environ)}
    key = (env.get("GEMINI_API_KEY") or "").strip() or None
    enabled = str(env.get("LLM_ENABLED", "")).strip().lower() in _TRUTHY
    provider = str(env.get("LLM_PROVIDER") or ("gemini" if key else "none")).strip().lower()
    if provider not in ("none", "gemini"):
        provider = "none"
    return LLMSettings(provider=provider, api_key=key, model=(env.get("LLM_MODEL") or DEFAULT_MODEL).strip(),
                       enabled=enabled)
