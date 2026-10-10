"""Implementation agents.

The shortener already exists, so "implementing" a feature here means VERIFYING
the real code: each ``impl_*`` agent calls the real functions with sample
inputs and records evidence ``{check, input, expected, actual, ok}``.

audit and retention have no code yet. Their agents publish a design and say so
(``design_only: true``, ``verified: false``); the only evidence they carry is
about the code they would build on.
"""
from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.models import AgentContext, AgentResult
from shortener import ratelimit, service, validation
from shortener.manifest import MANIFEST
from shortener.ratelimit import RateLimiter
from shortener.records import Click, UrlRecord
from shortener.service import UrlService
from shortener.store import UrlRepository, UrlStore

from .common import ManualClock, new_client, probe

REJECTED = "raised:invalid_url"


def _service(clock: ManualClock) -> UrlService:
    return UrlService(UrlStore(clock=clock), clock=clock, ip_salt="probe-salt")


def _part(ctx: AgentContext, name: str, feature: str, component: str, evidence: list[dict[str, Any]],
          design: dict[str, Any] | None = None) -> AgentResult:
    """Wrap evidence into the artifact ``impl_<name>``. A design-only part is never ``verified``."""
    ok = all(e["ok"] for e in evidence)
    components = {c["name"] for c in ctx.artifacts.get("architecture", {}).get("components", [])}
    content = {
        "feature": feature,
        "status": "ok" if ok else "failed",
        "verified": ok and design is None,
        "design_only": design is not None,
        "covered_by_architecture": component in components,
        "evidence": evidence,
    }
    if design is not None:
        content["design"] = design
    passed = sum(e["ok"] for e in evidence)
    return AgentResult(artifacts={f"impl_{name}": content}, summary=f"{passed}/{len(evidence)} checks ok")


# ---------------------------------------------------------------- greenfield
def impl_api(ctx: AgentContext) -> AgentResult:
    client, _ = new_client()
    url = "https://example.com/docs"
    created = client.post("/api/v1/urls", json={"url": url})
    code = created.json().get("code", "")
    get = lambda path: client.get(path, follow_redirects=False)  # noqa: E731
    evidence = [
        probe("health endpoint answers", "GET /health", 200, lambda: get("/health").status_code),
        probe("create returns 201", "POST /api/v1/urls", 201, lambda: created.status_code),
        probe("short code redirects with 302", f"GET /{code}", 302, lambda: get(f"/{code}").status_code),
        probe("redirect points at the destination", f"GET /{code}", url, lambda: get(f"/{code}").headers["location"]),
        probe("metadata reports status active", f"GET /api/v1/urls/{code}", "active",
              lambda: get(f"/api/v1/urls/{code}").json()["status"]),
        probe("unknown code uses the error format", "GET /api/v1/urls/nope", "not_found",
              lambda: get("/api/v1/urls/nope").json()["error"]["code"]),
        probe("delete returns 204", f"DELETE /api/v1/urls/{code}", 204,
              lambda: client.delete(f"/api/v1/urls/{code}").status_code),
        probe("deleted link answers 410", f"GET /{code}", 410, lambda: get(f"/{code}").status_code),
    ]
    return _part(ctx, "api", "api", "api", evidence)


def impl_store(ctx: AgentContext) -> AgentResult:
    clock = ManualClock()
    store = UrlStore(clock=clock)
    record = UrlRecord("abc1234", "https://example.com/a", clock())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "urls.json"
        UrlStore(path, clock=clock).add(record)
        reloaded = UrlStore(path, clock=clock).get("abc1234")
    evidence = [
        probe("add a new code", "add(abc1234)", True, lambda: store.add(record)),
        probe("adding the same code again is refused", "add(abc1234)", False, lambda: store.add(record)),
        probe("get returns the stored url", "get(abc1234)", record.url, lambda: store.get("abc1234").url),
        probe("click log keeps clicks", "add_click + get_clicks", 1,
              lambda: (store.add_click("abc1234", Click(clock(), None, "v1")), len(store.get_clicks("abc1234")))[1]),
        probe("soft delete keeps the record", "soft_delete(abc1234)", True,
              lambda: store.soft_delete("abc1234").deleted_at is not None and store.get("abc1234") is not None),
        probe("JSON file persistence survives a reload", "UrlStore(path) twice", record.url,
              lambda: reloaded.url),
    ]
    return _part(ctx, "store", "store", "store", evidence)


def impl_analytics(ctx: AgentContext) -> AgentResult:
    clock = ManualClock()
    svc = _service(clock)
    code = svc.create("https://example.com/a").record.code
    for ip, referrer in [("203.0.113.1", "https://news.example/"), ("203.0.113.1", "https://news.example/"),
                         ("203.0.113.2", None)]:
        svc.resolve_redirect(code, referrer, ip)
    stats = svc.analytics(code)
    digest = service.hash_visitor("203.0.113.1", "salt-a")
    evidence = [
        probe("total clicks counted", "3 redirects", 3, lambda: stats["total_clicks"]),
        probe("unique visitors counted by hash", "2 distinct client IPs", 2, lambda: stats["unique_visitors"]),
        probe("clicks grouped by UTC day", "all clicks on 2030-01-01", {"2030-01-01": 3},
              lambda: stats["clicks_by_day"]),
        probe("top referrer ranked", "news.example twice", {"referrer": "https://news.example/", "count": 2},
              lambda: stats["top_referrers"][0]),
        probe("visitor hash never contains the IP", "hash_visitor", True, lambda: "203.0.113.1" not in digest),
        probe("visitor hash has the documented length", "hash_visitor", service.VISITOR_HASH_LENGTH,
              lambda: len(digest)),
        probe("visitor hash depends on the salt", "salt-a vs salt-b", True,
              lambda: digest != service.hash_visitor("203.0.113.1", "salt-b")),
    ]
    return _part(ctx, "analytics", "analytics", "analytics", evidence)


# ---------------------------------------------------------------- brownfield
def impl_alias(ctx: AgentContext) -> AgentResult:
    svc = _service(ManualClock())
    evidence = [
        probe("accepts a valid alias", "my-link_1", "my-link_1", lambda: validation.validate_alias("my-link_1")),
        probe("rejects a too short alias", "ab", "raised:invalid_alias", lambda: validation.validate_alias("ab")),
        probe("rejects a too long alias", "a" * 33, "raised:invalid_alias",
              lambda: validation.validate_alias("a" * 33)),
        probe("rejects characters outside [A-Za-z0-9_-]", "has space", "raised:invalid_alias",
              lambda: validation.validate_alias("has space")),
        probe("rejects reserved words case-insensitively", "API", "raised:reserved_alias",
              lambda: validation.validate_alias("API")),
        probe("create stores the chosen alias", "alias=promo", "promo",
              lambda: svc.create("https://example.com/a", alias="promo").record.code),
        probe("a taken alias conflicts", "alias=promo again", "raised:alias_taken",
              lambda: svc.create("https://example.com/b", alias="promo")),
    ]
    return _part(ctx, "alias", "alias", "alias_policy", evidence)


def impl_ttl(ctx: AgentContext) -> AgentResult:
    clock = ManualClock()
    svc = _service(clock)
    code = svc.create("https://example.com/a", ttl_seconds=60).record.code
    record = UrlRecord("abc1234", "https://example.com/a", 100.0, expires_at=200.0)
    evidence = [
        probe("ttl of 0 is rejected", 0, "raised:invalid_ttl", lambda: validation.validate_ttl(0)),
        probe("ttl of one hour is accepted", 3600, 3600, lambda: validation.validate_ttl(3600)),
        probe("ttl above 365 days is rejected", validation.MAX_TTL_SECONDS + 1, "raised:invalid_ttl",
              lambda: validation.validate_ttl(validation.MAX_TTL_SECONDS + 1)),
        probe("not expired just before expires_at", 199.9, False, lambda: service.is_expired(record, 199.9)),
        probe("expired exactly at expires_at", 200.0, True, lambda: service.is_expired(record, 200.0)),
        probe("a link without expiry never expires", "expires_at=None", False,
              lambda: service.is_expired(UrlRecord("x", "https://example.com", 0.0), 10**12)),
        probe("redirect works before expiry", "t+0", "https://example.com/a",
              lambda: svc.resolve_redirect(code, None, "198.51.100.1")),
        probe("redirect is refused after expiry", "t+61s", "raised:expired",
              lambda: (clock.advance(61), svc.resolve_redirect(code, None, "198.51.100.1"))[1]),
    ]
    return _part(ctx, "ttl", "ttl", "expiry_policy", evidence)


def impl_ratelimit(ctx: AgentContext) -> AgentResult:
    clock = ManualClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    first_three = [limiter.check("a").allowed for _ in range(3)]
    fourth = limiter.check("a")
    client, _ = new_client(limit=2)
    statuses = [client.post("/api/v1/urls", json={"url": "https://example.com/a"}) for _ in range(3)]
    evidence = [
        probe("requests under the limit pass", "3 of 3", [True, True, True], lambda: first_three),
        probe("request over the limit is refused", "4th of 3", False, lambda: fourth.allowed),
        probe("retry_after covers the window", "4th of 3", 60, lambda: fourth.retry_after),
        probe("other clients are unaffected", "key b", True, lambda: limiter.check("b").allowed),
        probe("window expiry allows the client again", "t+61s", True,
              lambda: (clock.advance(61), limiter.check("a").allowed)[1]),
        probe("HTTP create answers 429 over the limit", "3rd POST with limit 2", 429,
              lambda: statuses[2].status_code),
        probe("429 carries Retry-After", "3rd POST with limit 2", True,
              lambda: "retry-after" in statuses[2].headers),
    ]
    return _part(ctx, "ratelimit", "rate_limit", "rate_limiter", evidence)


# ----------------------------------------------------------------- ambiguous
def impl_ssrf(ctx: AgentContext) -> AgentResult:
    blocked = ["http://127.0.0.1/admin", "http://localhost/", "http://2130706433/", "http://0x7f000001/",
               "http://[::1]/", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/",
               "http://service.internal/", "ftp://example.com/x", "javascript:alert(1)", "file:///etc/passwd",
               "https://user:pw@example.com/"]
    evidence = [probe("unsafe destination is rejected", dest, REJECTED,
                      lambda dest=dest: validation.validate_destination(dest)) for dest in blocked]
    evidence.append(probe("a normal https destination is accepted", "https://example.com/a?b=1",
                          "https://example.com/a?b=1",
                          lambda: validation.validate_destination("https://example.com/a?b=1")))
    result = _part(ctx, "ssrf", "ssrf", "destination_guard", evidence)
    result.artifacts["impl_ssrf"]["known_limitations"] = [
        x for x in MANIFEST["limits"]["known_limitations"] if "DNS" in x]
    return result


def impl_audit(ctx: AgentContext) -> AgentResult:
    design = {
        "events": ["url.created", "url.updated", "url.deleted"],
        "entry": "AuditEntry(timestamp, action, code, actor_hash)",
        "hook": "UrlService.create / update / delete, after the store call succeeds",
        "sink": "append-only JSON lines; never the raw API key or client IP",
        "not_built": "no audit code exists in the shortener yet",
    }
    evidence = [
        probe("click records hold no raw IP (audit can reuse the pattern)", "records.Click fields",
              ["timestamp", "referrer", "visitor_hash"], lambda: [f.name for f in dataclasses.fields(Click)]),
        probe("every mutation passes through UrlService", "create/update/delete", True,
              lambda: all(hasattr(UrlService, m) for m in ("create", "update", "delete"))),
    ]
    return _part(ctx, "audit", "audit", "audit_log", evidence, design)


def impl_retention(ctx: AgentContext) -> AgentResult:
    design = {
        "window": "90 days for clicks and soft-deleted links",
        "change": "add UrlRepository.purge(older_than) and call it from a scheduled task",
        "side_effect": "analytics totals shrink as clicks are purged",
        "not_built": "no retention code exists in the shortener yet",
    }
    evidence = [
        probe("clicks carry a timestamp to purge by", "records.Click", True,
              lambda: "timestamp" in {f.name for f in dataclasses.fields(Click)}),
        probe("records know when they were deleted", "records.UrlRecord", True,
              lambda: "deleted_at" in {f.name for f in dataclasses.fields(UrlRecord)}),
        probe("the repository has no purge yet (the design must add it)", "UrlRepository.purge", False,
              lambda: hasattr(UrlRepository, "purge")),
    ]
    return _part(ctx, "retention", "retention", "retention_job", evidence, design)


# ---------------------------------------------------------------------- sync
def sync_agent(ctx: AgentContext) -> AgentResult:
    """Merge every ``impl_*`` artifact into ``implementation`` and fail loudly if a part failed."""
    parts = {name.removeprefix("impl_"): art for name, art in ctx.artifacts.items() if name.startswith("impl_")}
    failed = sorted(n for n, p in parts.items() if p["status"] != "ok")
    content = {
        "status": "failed" if failed or not parts else "ok",
        "parts": {n: {"status": p["status"], "verified": p["verified"], "design_only": p["design_only"],
                      "checks": len(p["evidence"]), "checks_ok": sum(e["ok"] for e in p["evidence"])}
                  for n, p in parts.items()},
        "verified_parts": sorted(n for n, p in parts.items() if p["verified"]),
        "design_only_parts": sorted(n for n, p in parts.items() if p["design_only"]),
    }
    if content["status"] != "ok":
        raise RuntimeError(f"implementation failed: parts {failed or 'missing'} did not pass their checks")
    return AgentResult(artifacts={"implementation": content},
                       summary=f"{len(parts)} part(s) merged, {len(content['design_only_parts'])} design-only")


IMPL_AGENTS = {
    "impl_api": impl_api, "impl_store": impl_store, "impl_analytics": impl_analytics,
    "impl_alias": impl_alias, "impl_ttl": impl_ttl, "impl_ratelimit": impl_ratelimit,
    "impl_ssrf": impl_ssrf, "impl_audit": impl_audit, "impl_retention": impl_retention,
    "sync": sync_agent,
}
