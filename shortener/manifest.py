"""Plain-data description of the shortener, for tools (e.g. impact analysis).

Keep this in sync with the code: tests/shortener/test_manifest.py checks that
every module, function and endpoint listed here really exists.
"""

MANIFEST = {
    "package": "shortener",
    "entrypoint": "python -m shortener  (uvicorn on 127.0.0.1:8080)",
    "modules": {
        "app": {
            "purpose": "FastAPI app factory and HTTP routes; wires dependencies via app.state",
            "public": ["create_app", "router", "get_service", "enforce_rate_limit",
                       "rate_limit_key", "client_ip", "url_fields"],
        },
        "service": {
            "purpose": "Business logic: create (idempotent), redirect+click, update, delete, analytics",
            "public": ["UrlService", "generate_code", "is_expired", "link_status",
                       "hash_visitor", "request_fingerprint"],
            "constants": ["CODE_LENGTH", "MAX_CODE_ATTEMPTS", "MAX_REFERRER_LENGTH",
                          "VISITOR_HASH_LENGTH", "TOP_REFERRERS_LIMIT", "UPDATABLE_FIELDS"],
        },
        "validation": {
            "purpose": "Destination URL (SSRF/scheme guard), alias and ttl rules",
            "public": ["validate_destination", "validate_alias", "validate_ttl",
                       "parse_ipv4_literal"],
            "constants": ["MAX_URL_LENGTH", "ALLOWED_SCHEMES", "BLOCKED_HOSTNAMES",
                          "BLOCKED_HOST_SUFFIXES", "ALIAS_MIN_LENGTH", "ALIAS_MAX_LENGTH",
                          "RESERVED_ALIASES", "MIN_TTL_SECONDS", "MAX_TTL_SECONDS"],
        },
        "store": {
            "purpose": "Storage interface (UrlRepository) and in-memory/JSON-file UrlStore",
            "public": ["UrlRepository", "UrlStore"],
        },
        "ratelimit": {
            "purpose": "Sliding-window rate limiter with injectable clock",
            "public": ["RateLimiter", "RateLimitDecision"],
            "constants": ["DEFAULT_LIMIT", "DEFAULT_WINDOW_SECONDS"],
        },
        "schemas": {
            "purpose": "Pydantic request/response models (OpenAPI source)",
            "public": ["CreateUrlRequest", "UpdateUrlRequest", "UrlResponse",
                       "UrlMetadataResponse", "AnalyticsResponse", "ReferrerCount",
                       "HealthResponse", "ErrorResponse", "to_iso"],
        },
        "records": {
            "purpose": "Frozen dataclasses stored by the store",
            "public": ["UrlRecord", "Click", "IdempotencyEntry"],
        },
        "errors": {
            "purpose": "AppError hierarchy and the {error:{code,message}} JSON handlers",
            "public": ["AppError", "ValidationFailed", "NotFound", "Gone", "Conflict",
                       "RateLimited", "ServiceUnavailable", "install_error_handlers"],
        },
        "clock": {
            "purpose": "Clock type (callable returning epoch seconds) and system clock",
            "public": ["Clock", "system_clock"],
        },
        "manifest": {"purpose": "This description", "public": ["MANIFEST"]},
    },
    "endpoints": [
        {"method": "GET", "path": "/health", "success": 200,
         "handler": "app.health", "errors": []},
        {"method": "POST", "path": "/api/v1/urls", "success": 201,
         "handler": "app.create_url", "rate_limited": True,
         "headers": ["Idempotency-Key", "X-API-Key"],
         "notes": "200 on idempotent replay",
         "errors": [409, 422, 429, 503]},
        {"method": "GET", "path": "/api/v1/urls/{code}", "success": 200,
         "handler": "app.get_url", "errors": [404, 410]},
        {"method": "PATCH", "path": "/api/v1/urls/{code}", "success": 200,
         "handler": "app.update_url", "errors": [404, 410, 422]},
        {"method": "DELETE", "path": "/api/v1/urls/{code}", "success": 204,
         "handler": "app.delete_url", "notes": "soft delete", "errors": [404, 410]},
        {"method": "GET", "path": "/api/v1/urls/{code}/analytics", "success": 200,
         "handler": "app.get_analytics", "errors": [404, 410]},
        {"method": "GET", "path": "/{code}", "success": 302,
         "handler": "app.redirect", "notes": "records a click; must be registered last",
         "errors": [404, 410]},
    ],
    "data_fields": {
        "UrlRecord": ["code", "url", "created_at", "expires_at", "deleted_at"],
        "Click": ["timestamp", "referrer", "visitor_hash"],
        "IdempotencyEntry": ["fingerprint", "record"],
        "CreateUrlRequest": ["url", "alias", "ttl_seconds"],
        "UpdateUrlRequest": ["url", "ttl_seconds"],
        "UrlResponse": ["code", "short_url", "url", "expires_at", "created_at"],
        "UrlMetadataResponse": ["code", "short_url", "url", "expires_at", "created_at", "status"],
        "AnalyticsResponse": ["total_clicks", "clicks_by_day", "top_referrers",
                              "unique_visitors"],
        "ErrorResponse": ["error.code", "error.message"],
    },
    "error_codes": {
        "invalid_url": 422, "invalid_alias": 422, "reserved_alias": 422, "invalid_ttl": 422,
        "invalid_update": 422, "invalid_idempotency_key": 422, "validation_error": 422,
        "alias_taken": 409, "idempotency_key_conflict": 409,
        "not_found": 404, "deleted": 410, "expired": 410,
        "rate_limited": 429, "code_generation_failed": 503,
    },
    "limits": {
        "rate_limit": "30 requests / 60 s per X-API-Key (else client IP), POST /api/v1/urls only",
        "known_limitations": [
            "No DNS resolution: DNS rebinding / hostnames resolving to private IPs not detected",
            "Rate limiter, idempotency keys and clicks are per-process / per-store",
            "JSON persistence rewrites the whole file on every change",
            "Idempotency keys are global (not scoped per client) and never expire",
        ],
    },
}
