"""Business logic: creating, resolving, updating and analysing short links.

The service knows nothing about HTTP. It talks to a UrlRepository and raises
AppError subclasses, which app.py turns into responses.
"""
import hashlib
import json
import secrets
import string
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from shortener.clock import Clock, system_clock
from shortener.errors import Conflict, Gone, NotFound, ServiceUnavailable, ValidationFailed
from shortener.records import Click, IdempotencyEntry, UrlRecord
from shortener.store import UrlRepository
from shortener.validation import validate_alias, validate_destination, validate_ttl

BASE62_ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7
MAX_CODE_ATTEMPTS = 10  # random-code collisions are rare; give up after this many
MAX_IDEMPOTENCY_KEY_LENGTH = 255
MAX_REFERRER_LENGTH = 2048
VISITOR_HASH_LENGTH = 16  # hex chars kept from the sha256 digest
TOP_REFERRERS_LIMIT = 10
UPDATABLE_FIELDS = frozenset({"url", "ttl_seconds"})

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_DELETED = "deleted"


# --- small pure helpers ------------------------------------------------------
def generate_code(length: int = CODE_LENGTH) -> str:
    """Random base62 code from a cryptographically secure source."""
    return "".join(secrets.choice(BASE62_ALPHABET) for _ in range(length))


def is_expired(record: UrlRecord, now: float) -> bool:
    return record.expires_at is not None and now >= record.expires_at


def link_status(record: UrlRecord, now: float) -> str:
    if record.deleted_at is not None:
        return STATUS_DELETED
    return STATUS_EXPIRED if is_expired(record, now) else STATUS_ACTIVE


def hash_visitor(client_ip: str, salt: str) -> str:
    """Salted, truncated sha256 of the client IP. The raw IP is never stored."""
    digest = hashlib.sha256(f"{salt}:{client_ip}".encode("utf-8")).hexdigest()
    return digest[:VISITOR_HASH_LENGTH]


def request_fingerprint(url: str, alias: str | None, ttl_seconds: int | None) -> str:
    """Stable hash of a create request body, used to compare idempotent retries."""
    body = {"url": url, "alias": alias, "ttl_seconds": ttl_seconds}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CreateResult:
    record: UrlRecord
    created: bool  # False when an idempotent retry returned the earlier result


class UrlService:
    def __init__(
        self,
        store: UrlRepository,
        clock: Clock = system_clock,
        ip_salt: str | None = None,
        code_generator: Callable[[], str] = generate_code,
    ):
        self._store = store
        self._clock = clock
        # A random salt means visitor hashes change after a restart unless
        # SHORTENER_IP_SALT is configured (see app.create_app).
        self._ip_salt = ip_salt or secrets.token_hex(16)
        self._code_generator = code_generator
        self._create_lock = threading.Lock()  # makes "check key, then insert" atomic

    # --- create ---------------------------------------------------------------
    def create(
        self,
        url: str,
        alias: str | None = None,
        ttl_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> CreateResult:
        if idempotency_key is not None and not (
            0 < len(idempotency_key) <= MAX_IDEMPOTENCY_KEY_LENGTH
        ):
            raise ValidationFailed(
                "invalid_idempotency_key",
                f"Idempotency-Key must be 1-{MAX_IDEMPOTENCY_KEY_LENGTH} characters",
            )
        fingerprint = request_fingerprint(url, alias, ttl_seconds)

        with self._create_lock:
            if idempotency_key is not None:
                replay = self._replay(idempotency_key, fingerprint)
                if replay is not None:
                    return CreateResult(replay, created=False)

            url = validate_destination(url)
            if ttl_seconds is not None:
                validate_ttl(ttl_seconds)
            now = self._clock()
            expires_at = now + ttl_seconds if ttl_seconds is not None else None

            if alias is not None:
                record = UrlRecord(validate_alias(alias), url, now, expires_at)
                if not self._store.add(record):
                    raise Conflict("alias_taken", f"alias '{alias}' is already in use")
            else:
                record = self._add_with_random_code(url, now, expires_at)

            if idempotency_key is not None:
                self._store.put_idempotency(idempotency_key, IdempotencyEntry(fingerprint, record))
            return CreateResult(record, created=True)

    def _replay(self, key: str, fingerprint: str) -> UrlRecord | None:
        entry = self._store.get_idempotency(key)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint:
            raise Conflict(
                "idempotency_key_conflict",
                "this Idempotency-Key was already used with a different request body",
            )
        return entry.record

    def _add_with_random_code(self, url: str, now: float, expires_at: float | None) -> UrlRecord:
        for _ in range(MAX_CODE_ATTEMPTS):
            record = UrlRecord(self._code_generator(), url, now, expires_at)
            if self._store.add(record):
                return record
        raise ServiceUnavailable("code_generation_failed", "could not generate a unique code")

    # --- read -----------------------------------------------------------------
    def get(self, code: str) -> UrlRecord:
        """Return the record, whether active or expired. 404 unknown, 410 deleted."""
        record = self._store.get(code)
        if record is None:
            raise NotFound("not_found", f"no short link with code '{code}'")
        if record.deleted_at is not None:
            raise Gone("deleted", "this link has been deleted")
        return record

    def status_of(self, record: UrlRecord) -> str:
        return link_status(record, self._clock())

    def resolve_redirect(self, code: str, referrer: str | None, client_ip: str) -> str:
        """Return the destination for a click and record the click."""
        record = self.get(code)
        now = self._clock()
        if is_expired(record, now):
            raise Gone("expired", "this link has expired")
        click = Click(
            timestamp=now,
            referrer=referrer[:MAX_REFERRER_LENGTH] if referrer else None,
            visitor_hash=hash_visitor(client_ip, self._ip_salt),
        )
        self._store.add_click(code, click)
        return record.url

    # --- update / delete ----------------------------------------------------------
    def update(self, code: str, changes: dict[str, Any]) -> UrlRecord:
        """Apply a PATCH. `changes` holds only the fields the client sent."""
        unknown = set(changes) - UPDATABLE_FIELDS
        if not changes or unknown:
            raise ValidationFailed("invalid_update", "send at least one of: url, ttl_seconds")
        self.get(code)  # 404 / 410 checks

        fields: dict[str, Any] = {}
        if "url" in changes:
            if changes["url"] is None:
                raise ValidationFailed("invalid_url", "url cannot be null")
            fields["url"] = validate_destination(changes["url"])
        if "ttl_seconds" in changes:
            ttl = changes["ttl_seconds"]
            fields["expires_at"] = None if ttl is None else self._clock() + validate_ttl(ttl)

        updated = self._store.update(code, **fields)
        return updated  # type: ignore[return-value]  # get() above proved it exists

    def delete(self, code: str) -> None:
        self.get(code)
        self._store.soft_delete(code)

    # --- analytics -------------------------------------------------------------
    def analytics(self, code: str) -> dict[str, Any]:
        self.get(code)
        clicks = self._store.get_clicks(code)

        by_day = Counter(
            datetime.fromtimestamp(c.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            for c in clicks
        )
        referrers = Counter(c.referrer for c in clicks if c.referrer)
        # Sort by count (high first), then name, so the output is deterministic.
        top = sorted(referrers.items(), key=lambda item: (-item[1], item[0]))[:TOP_REFERRERS_LIMIT]
        return {
            "total_clicks": len(clicks),
            "clicks_by_day": dict(sorted(by_day.items())),
            "top_referrers": [{"referrer": name, "count": count} for name, count in top],
            "unique_visitors": len({c.visitor_hash for c in clicks}),
        }
