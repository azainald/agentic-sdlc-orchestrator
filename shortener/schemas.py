"""Pydantic request/response models (these drive the OpenAPI document)."""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from shortener.validation import MAX_TTL_SECONDS, MIN_TTL_SECONDS


def to_iso(timestamp: float | None) -> str | None:
    """Epoch seconds -> '2030-01-01T12:00:00Z' (UTC), keeping None as None."""
    if timestamp is None:
        return None
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class CreateUrlRequest(BaseModel):
    # Detailed rules (SSRF checks, alias rules) are enforced in validation.py.
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Destination http(s) URL.", examples=["https://example.com/page"])
    alias: str | None = Field(
        default=None, description="Optional custom short code (3-32 chars: A-Z a-z 0-9 _ -)."
    )
    ttl_seconds: int | None = Field(
        default=None,
        ge=MIN_TTL_SECONDS,
        le=MAX_TTL_SECONDS,
        description="Optional lifetime in seconds; the link expires afterwards.",
    )


class UpdateUrlRequest(BaseModel):
    """Send only the fields to change. ttl_seconds=null removes the expiry."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, description="New destination URL.")
    ttl_seconds: int | None = Field(
        default=None,
        ge=MIN_TTL_SECONDS,
        le=MAX_TTL_SECONDS,
        description="New lifetime counted from now; null means 'never expires'.",
    )


class UrlResponse(BaseModel):
    code: str
    short_url: str
    url: str
    expires_at: str | None = Field(description="UTC ISO-8601 time, or null if it never expires.")
    created_at: str


class UrlMetadataResponse(UrlResponse):
    status: str = Field(description="'active' or 'expired' (deleted links answer 410).")


class ReferrerCount(BaseModel):
    referrer: str
    count: int


class AnalyticsResponse(BaseModel):
    total_clicks: int
    clicks_by_day: dict[str, int] = Field(description="UTC day (YYYY-MM-DD) -> clicks.")
    top_referrers: list[ReferrerCount]
    unique_visitors: int = Field(description="Distinct hashed client IPs.")


class HealthResponse(BaseModel):
    status: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
