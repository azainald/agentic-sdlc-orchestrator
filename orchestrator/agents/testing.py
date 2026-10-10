"""Testing agents: the real shortener test-suite plus a live in-process smoke test.

Params (all optional):
    inject_failure   "testing"      the primary agent fails on attempt 1 only (retry succeeds)
                     "testing_hard" the primary agent always fails (fallback is used)
                     "testing_all"  the fallback fails too (rollback and safe-stop)
    skip_pytest      True = do not launch the pytest subprocess (used by unit tests, which are
                     already running inside pytest)
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Any

from orchestrator.models import AgentContext, AgentResult

from .common import new_client, probe, shortener_root

PYTEST_TIMEOUT_S = 300
SUMMARY_COUNT = re.compile(r"(\d+) (passed|failed|error)")


def _suite(name: str, kind: str, evidence: list[dict[str, Any]], started: float) -> dict[str, Any]:
    return {"name": name, "kind": kind, "passed": sum(e["ok"] for e in evidence),
            "failed": sum(not e["ok"] for e in evidence), "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "checks": evidence}


def run_pytest(root) -> dict[str, Any]:
    """Run tests/shortener in a subprocess and read the counts from pytest's summary line."""
    started = time.perf_counter()
    cmd = [sys.executable, "-m", "pytest", "tests/shortener", "-q", "--tb=no", "-p", "no:cacheprovider"]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S)
    counts = {"passed": 0, "failed": 0, "error": 0}
    for number, word in SUMMARY_COUNT.findall(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""):
        counts[word] = int(number)
    failed = counts["failed"] + counts["error"]
    if proc.returncode != 0 and failed == 0:
        failed = 1  # pytest failed without a readable summary (collection error, crash): never count as green
    return {"name": "pytest tests/shortener", "kind": "pytest", "passed": counts["passed"], "failed": failed,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1), "exit_code": proc.returncode}


def _error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _redirect(response) -> tuple[int, str]:
    return response.status_code, response.headers["location"]


def smoke_checks(full: bool = True) -> list[dict[str, Any]]:
    """Drive the real FastAPI app end to end with a manual clock (no sleeping)."""
    client, clock = new_client()
    get = lambda path, **kw: client.get(path, follow_redirects=False, **kw)  # noqa: E731
    url = "https://example.com/landing"
    created = client.post("/api/v1/urls", json={"url": url, "ttl_seconds": 60})
    code = created.json().get("code", "")
    checks = [
        probe("health is ok", "GET /health", "ok", lambda: get("/health").json()["status"]),
        probe("create answers 201", "POST /api/v1/urls", 201, lambda: created.status_code),
        probe("redirect goes to the destination", f"GET /{code}", (302, url),
              lambda: _redirect(get(f"/{code}"))),
    ]
    if not full:
        return checks
    checks += [
        probe("analytics count the click", f"GET /api/v1/urls/{code}/analytics", 1,
              lambda: get(f"/api/v1/urls/{code}/analytics").json()["total_clicks"]),
        probe("private destination is rejected", "POST url=http://127.0.0.1/", (422, "invalid_url"),
              lambda: _error(client.post("/api/v1/urls", json={"url": "http://127.0.0.1/"}))),
        probe("expired link answers 410", "t+61s", (410, "expired"),
              lambda: (clock.advance(61), _error(get(f"/{code}")))[1]),
    ]
    return checks


def _report(suites: list[dict[str, Any]], started: float) -> AgentResult:
    passed, failed = sum(s["passed"] for s in suites), sum(s["failed"] for s in suites)
    report = {"passed": passed, "failed": failed, "suites": suites,
              "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
    return AgentResult(artifacts={"test_report": report},
                       summary=f"{passed} passed, {failed} failed in {len(suites)} suite(s)")


def testing_agent(ctx: AgentContext) -> AgentResult:
    mode = ctx.params.get("inject_failure")
    if mode == "testing" and ctx.attempt == 1 and not ctx.is_fallback:
        raise RuntimeError("injected transient test failure")
    if mode in ("testing_hard", "testing_all") and not ctx.is_fallback:
        raise RuntimeError("injected permanent test failure")

    started, suites = time.perf_counter(), []
    if not ctx.params.get("skip_pytest", False):
        suites.append(run_pytest(shortener_root(ctx)))
    suite_start = time.perf_counter()
    suites.append(_suite("in-process smoke (TestClient)", "smoke", smoke_checks(), suite_start))
    return _report(suites, started)


def testing_fallback_agent(ctx: AgentContext) -> AgentResult:
    """Cheaper second opinion: three in-process checks, no subprocess."""
    if ctx.params.get("inject_failure") == "testing_all":
        raise RuntimeError("injected fallback failure")
    started = time.perf_counter()
    return _report([_suite("fallback smoke (health, create, redirect)", "smoke",
                           smoke_checks(full=False), started)], started)


def rollback_testing_agent(ctx: AgentContext) -> AgentResult:
    """Testing publishes nothing until it passes, so there is little to undo. Record that fact
    explicitly, because 'nothing was published' is what keeps the release gate closed."""
    published = "test_report" in ctx.artifacts
    undone = ["no test_report was published, so the release gate stays closed" if not published
              else "a stale test_report from an earlier execution is left in place"]
    return AgentResult(summary=f"testing rolled back ({ctx.params.get('failure')}); " + "; ".join(undone))
