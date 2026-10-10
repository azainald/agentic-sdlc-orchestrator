"""Plain data records kept by the store.

They are frozen (immutable): "changing" a record means creating a modified
copy with dataclasses.replace, which keeps the store thread-safe and simple.
Times are Unix epoch seconds (float).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class UrlRecord:
    code: str
    url: str
    created_at: float
    expires_at: float | None = None
    deleted_at: float | None = None


@dataclass(frozen=True)
class Click:
    timestamp: float
    referrer: str | None
    visitor_hash: str  # truncated salted sha256 of the client IP, never the IP


@dataclass(frozen=True)
class IdempotencyEntry:
    fingerprint: str  # hash of the request body that first used the key
    record: UrlRecord  # snapshot of the result, so a replay returns the same payload
