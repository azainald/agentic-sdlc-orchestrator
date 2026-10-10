"""The knowledge base the agents reason with: one row per product feature.

Requirements, architecture and impact agents all read this table, so adding a
feature is one new ``Feature(...)`` entry and every agent picks it up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from shortener import ratelimit, validation


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    pattern: str                                  # regex that detects the feature in free text
    goal: str
    criteria: tuple[str, ...]                     # Given/When/Then acceptance criteria
    component: tuple[str, str, str]               # (name, module, responsibility)
    data_model: tuple[str, ...] = ()
    api_deltas: tuple[str, ...] = ()
    decision: tuple[str, str, str, str] = ("", "", "", "")  # (title, context, decision, consequence)
    handlers: tuple[str, ...] = ()                # entry points in the shortener, "module.function"
    targets: tuple[str, ...] = ()                 # code this feature relies on, "module.Class.method"
    fields: tuple[str, ...] = ()                  # keys of MANIFEST["data_fields"] it touches
    risks: tuple[str, ...] = ()
    public_api: bool = False                      # changes the public HTTP contract
    design_only: bool = False                     # no implementation exists in the shortener yet
    non_goals: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


FEATURES: dict[str, Feature] = {f.key: f for f in [
    Feature(
        key="alias", label="custom aliases", pattern=r"alias|custom (code|slug|short)",
        goal="Let callers choose a memorable short code instead of a random one",
        criteria=(
            "Given the alias 'promo' is free, When POST /api/v1/urls is sent with it, "
            "Then the response is 201 and the short URL ends with /promo",
            "Given the alias 'promo' is taken, When another create uses it, Then the response is 409 alias_taken",
            "Given a reserved word such as 'api' or a malformed alias, When create is called, "
            "Then the response is 422 (reserved_alias / invalid_alias)",
        ),
        component=("alias_policy", "validation", "Alias format, length and reserved-word rules"),
        data_model=("CreateUrlRequest.alias (optional string)", "UrlRecord.code may be caller-chosen"),
        api_deltas=("POST /api/v1/urls accepts an optional 'alias'; adds 409 alias_taken, "
                    "422 invalid_alias and reserved_alias",),
        decision=("Validate aliases in one module",
                  "Aliases share the URL namespace with routes such as /health and /api",
                  "validate_alias enforces length, charset and a reserved-word list before the store is touched",
                  "New routes must be added to RESERVED_ALIASES or they become hijackable"),
        handlers=("app.create_url",),
        targets=("validation.validate_alias", "store.UrlStore.add"),
        fields=("CreateUrlRequest", "UrlRecord"),
        risks=("A new top-level route silently collides with an existing alias",
               "Case handling: reserved words are matched case-insensitively, aliases are case-sensitive"),
        public_api=True,
        non_goals=("Renaming an alias after creation",),
        assumptions=(f"Aliases are {validation.ALIAS_MIN_LENGTH}-{validation.ALIAS_MAX_LENGTH} "
                     "characters of letters, digits, '_' and '-'",),
    ),
    Feature(
        key="ttl", label="link expiry (ttl)", pattern=r"\bttl\b|expir|time[- ]to[- ]live|lifetime",
        goal="Let links stop working after a chosen time",
        criteria=(
            "Given a link created with ttl_seconds=60, When 61 seconds pass and the short URL is visited, "
            "Then the response is 410 expired",
            "Given ttl_seconds outside 1..31536000, When create is called, Then the response is 422 invalid_ttl",
            "Given a link without ttl_seconds, When any amount of time passes, Then it never expires",
        ),
        component=("expiry_policy", "service", "Computes expires_at and decides whether a link is expired"),
        data_model=("UrlRecord.expires_at (epoch seconds, nullable)", "CreateUrlRequest.ttl_seconds"),
        api_deltas=("POST and PATCH accept ttl_seconds; GET /{code} returns 410 expired after expiry",),
        decision=("Compute expiry on read, never by background job",
                  "A sweeper would add scheduling, locking and a second source of truth",
                  "is_expired(record, now) is evaluated on every redirect and metadata read with an injectable clock",
                  "Expired records stay in the store (analytics remain readable) until a retention policy removes them"),
        handlers=("app.create_url", "app.update_url", "app.redirect"),
        targets=("validation.validate_ttl", "service.is_expired", "service.UrlService.resolve_redirect"),
        fields=("UrlRecord", "CreateUrlRequest", "UpdateUrlRequest"),
        risks=("Clock skew between processes changes when a link expires",
               "PATCH ttl_seconds counts from 'now', which can surprise clients"),
        public_api=True,
        non_goals=("Automatic deletion of expired links",),
        assumptions=(f"The maximum ttl is {validation.MAX_TTL_SECONDS} seconds (365 days)",),
    ),
    Feature(
        key="analytics", label="click analytics", pattern=r"analytic|click|statistic|visitor|referrer",
        goal="Show owners how their links are used without storing personal data",
        criteria=(
            "Given a link visited twice from two clients, When GET /api/v1/urls/{code}/analytics is called, "
            "Then total_clicks is 2 and unique_visitors is 2",
            "Given clicks with referrers, When analytics are read, Then top_referrers is sorted by count then name",
            "Given a click is recorded, When the store is inspected, Then it holds a salted hash and never the raw IP",
        ),
        component=("analytics", "service", "Aggregates clicks by day, referrer and unique visitor hash"),
        data_model=("Click(timestamp, referrer, visitor_hash)", "UrlStore click log per code"),
        api_deltas=("GET /api/v1/urls/{code}/analytics returns total_clicks, clicks_by_day, "
                    "top_referrers, unique_visitors",),
        decision=("Hash visitors with a salted, truncated sha256",
                  "Unique-visitor counts are needed but raw IPs are personal data",
                  "hash_visitor(ip, salt) is stored per click; the IP itself is never persisted",
                  "Hashes change after a restart unless SHORTENER_IP_SALT is configured"),
        handlers=("app.redirect", "app.get_analytics"),
        targets=("service.UrlService.analytics", "service.hash_visitor", "store.UrlStore.add_click"),
        fields=("Click", "AnalyticsResponse"),
        risks=("Analytics are computed from the full click list on every request",
               "Clicks are per-process unless the JSON store is used"),
        public_api=True,
        non_goals=("Per-click geolocation", "Real-time dashboards"),
        assumptions=("Visitor identity is approximated by a hashed client IP",),
    ),
    Feature(
        key="rate_limit", label="rate limiting", pattern=r"rate[- ]?limit|throttl",
        goal="Protect link creation from abuse",
        criteria=(
            "Given a client has sent 30 create requests in 60 seconds, When it sends another, "
            "Then the response is 429 rate_limited with a Retry-After header",
            "Given two different X-API-Key values, When one is limited, Then the other is still served",
            "Given the window has passed, When the client retries, Then the request is allowed again",
        ),
        component=("rate_limiter", "ratelimit", "Sliding-window limiter keyed by API key or client IP"),
        data_model=("In-memory hit timestamps per client key",),
        api_deltas=("POST /api/v1/urls may answer 429 with a Retry-After header",),
        decision=("In-process sliding window with an injectable clock",
                  "One instance is enough for the target deployment and keeps the limiter testable",
                  f"RateLimiter allows {ratelimit.DEFAULT_LIMIT} requests per {int(ratelimit.DEFAULT_WINDOW_SECONDS)}s per key",
                  "Limits are per process; a multi-instance deployment needs a shared store such as Redis"),
        handlers=("app.create_url",),
        targets=("app.enforce_rate_limit", "ratelimit.RateLimiter.check"),
        fields=(),
        risks=("Limits are not shared between processes", "Clients can rotate X-API-Key values to evade the limit"),
        public_api=True,
        non_goals=("Distributed rate limiting",),
        assumptions=("Only link creation is rate limited",),
    ),
    Feature(
        key="ssrf", label="destination safety (SSRF guard)",
        pattern=r"ssrf|private (ip|address)|internal (host|address)|localhost|javascript:|malicious (url|destination)|phishing",
        goal="Refuse destinations that point at internal infrastructure or use dangerous schemes",
        criteria=(
            "Given a destination on a loopback, private or link-local address, When create is called, "
            "Then the response is 422 invalid_url",
            "Given a javascript:, data: or file: destination, When create is called, Then the response is 422",
            "Given a normal https destination, When create is called, Then the response is 201",
        ),
        component=("destination_guard", "validation", "Scheme, host and IP-literal checks for destinations"),
        data_model=(),
        api_deltas=("POST and PATCH reject unsafe destinations with 422 invalid_url",),
        decision=("Validate syntactically, do not resolve DNS",
                  "Resolving DNS inside the API adds latency and a TOCTOU window",
                  "validate_destination blocks schemes, credentials, odd IPv4 encodings and private ranges",
                  "DNS rebinding is not covered; the component that follows the URL must re-check at connect time"),
        handlers=("app.create_url", "app.update_url"),
        targets=("validation.validate_destination",),
        fields=("CreateUrlRequest",),
        risks=("Hostnames that resolve to private IPs pass the syntactic check",),
        non_goals=("Reputation or malware scanning of destinations",),
        assumptions=("Only http and https destinations are ever allowed",),
    ),
    Feature(
        key="audit", label="audit logging", pattern=r"audit",
        goal="Keep an accountable record of who created, changed or deleted a link",
        criteria=(
            "Given a link is created, updated or deleted, When the audit log is read, "
            "Then one entry names the action, the code and a timestamp",
            "Given an audit entry, When it is inspected, Then it contains no raw IP address and no API key",
        ),
        component=("audit_log", "service", "Append-only record of mutating actions (design only)"),
        data_model=("AuditEntry(timestamp, action, code, actor_hash) - new record, not implemented",),
        api_deltas=(),
        decision=("Append-only audit entries written by the service layer",
                  "Mutations all pass through UrlService, so one place can observe all of them",
                  "Each mutation appends an AuditEntry with a hashed actor, never the raw key or IP",
                  "Needs a durable sink; the JSON store rewrites the whole file on each change"),
        handlers=("app.create_url", "app.update_url", "app.delete_url"),
        targets=("service.UrlService.create", "service.UrlService.update", "service.UrlService.delete"),
        fields=("UrlRecord",),
        risks=("Audit writes add latency to every mutation", "Audit data is itself personal data"),
        design_only=True,
        non_goals=("Tamper-proof storage",),
        assumptions=("Audit entries are hashed actors, not identities",),
    ),
    Feature(
        key="retention", label="data retention", pattern=r"retention|retain|purge|gdpr",
        goal="Delete click and link data once it is no longer needed",
        criteria=(
            "Given clicks older than the retention window, When the retention job runs, Then they are removed",
            "Given a soft-deleted link past the window, When the job runs, Then the record is purged",
        ),
        component=("retention_job", "store", "Periodic purge of old clicks and deleted links (design only)"),
        data_model=("Retention window setting", "Click.timestamp is the purge key"),
        api_deltas=(),
        decision=("Purge by age through the repository interface",
                  "Click.timestamp already exists, so no schema change is needed",
                  "A purge(older_than) method on UrlRepository removes expired clicks and deleted links",
                  "Analytics totals shrink when clicks are purged"),
        handlers=(),
        targets=("store.UrlStore.get_clicks", "store.UrlStore.soft_delete"),
        fields=("Click", "UrlRecord"),
        risks=("Purging changes analytics results", "The JSON file store rewrites everything on each purge"),
        design_only=True,
        non_goals=("Per-customer retention policies",),
        assumptions=("One global retention window",),
    ),
]}

# Terms that sound like a requirement but name no behaviour. "Make links safer" has only these.
VAGUE_PATTERN = re.compile(r"\b(safe\w*|secur\w*|protect\w*|harden\w*|better|improve\w*|robust\w*|reliab\w*)\b", re.I)
SECURITY_VAGUE = re.compile(r"safe|secur|protect|harden", re.I)


def detect_features(text: str) -> list[str]:
    """Keys of every feature whose pattern appears in ``text`` (table order)."""
    return [key for key, f in FEATURES.items() if re.search(f.pattern, text, re.I)]


def vague_terms(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in VAGUE_PATTERN.finditer(text)})
