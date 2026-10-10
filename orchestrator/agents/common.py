"""Small helpers shared by the agents."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from orchestrator.models import AgentContext
from shortener.app import create_app
from shortener.ratelimit import DEFAULT_LIMIT, RateLimiter
from shortener.store import UrlStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def shortener_root(ctx: AgentContext) -> Path:
    """Directory that contains ``shortener/`` and ``tests/shortener/``.

    ``params["shortener_root"]`` wins; the default is this repository."""
    return Path(ctx.params.get("shortener_root") or PROJECT_ROOT)


def effective_requirements(ctx: AgentContext) -> dict[str, Any]:
    """The requirements downstream agents should design against: the human-approved
    ``normalized_requirements`` when the ambiguity branch produced them."""
    return ctx.artifacts.get("normalized_requirements") or ctx.artifacts.get("requirements") or {}


def features_of(ctx: AgentContext) -> list[str]:
    return list(effective_requirements(ctx).get("detected_features", []))


class ManualClock:
    """A clock tests and probes move by hand (the shortener takes any ``() -> float``)."""

    def __init__(self, start: float = 1_893_456_000.0):  # 2030-01-01T00:00:00Z
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def new_client(limit: int = DEFAULT_LIMIT) -> tuple[TestClient, ManualClock]:
    """A fresh, isolated shortener app (in-memory store) driven by a manual clock."""
    clock = ManualClock()
    app = create_app(store=UrlStore(clock=clock), limiter=RateLimiter(limit=limit, clock=clock),
                     clock=clock, ip_salt="probe-salt")
    return TestClient(app), clock


def probe(check: str, given: Any, expected: Any, fn: Callable[[], Any]) -> dict[str, Any]:
    """Run ``fn`` against the real code and record the evidence.

    An exception counts as the result ``"raised:<code or class>"`` so that
    "this input must be rejected" is expressed as ``expected="raised:invalid_alias"``."""
    try:
        actual = fn()
    except Exception as exc:
        actual = f"raised:{getattr(exc, 'code', type(exc).__name__)}"
    return {"check": check, "input": given, "expected": expected, "actual": actual, "ok": actual == expected}
