"""Input validation: destination URLs (SSRF guard), aliases and TTLs.

All rules live here as module-level constants and small functions, so they are
easy to find, test and change. Every function raises ValidationFailed.

Known limitation: no DNS resolution is done. A public-looking hostname that
resolves to a private IP (or a DNS-rebinding attack) is NOT caught here; that
would need a check at connect time in whatever component actually fetches or
follows the URL.
"""
import ipaddress
import re
from urllib.parse import urlsplit

from shortener.errors import ValidationFailed

# --- destination rules -------------------------------------------------------
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
BLOCKED_HOSTNAMES = frozenset({"localhost"})
BLOCKED_HOST_SUFFIXES = (".localhost", ".internal", ".local")

# --- alias rules -------------------------------------------------------------
ALIAS_MIN_LENGTH = 3
ALIAS_MAX_LENGTH = 32
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# Paths that already mean something; "redoc" is served by FastAPI like "docs".
RESERVED_ALIASES = frozenset({"api", "admin", "health", "docs", "redoc", "openapi.json"})

# --- ttl rules ---------------------------------------------------------------
MIN_TTL_SECONDS = 1
MAX_TTL_SECONDS = 31_536_000  # 365 days

_HOST_LABEL = re.compile(r"[a-z0-9_-]{1,63}")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
_HEX_LABEL = re.compile(r"0[xX][0-9a-fA-F]*")
_OCTAL_LABEL = re.compile(r"0[0-7]*")
_DECIMAL_LABEL = re.compile(r"[1-9][0-9]*")


def _invalid_url(message: str) -> ValidationFailed:
    return ValidationFailed("invalid_url", message)


# --- destination -------------------------------------------------------------
def validate_destination(url: str) -> str:
    """Return the (stripped) URL if it is safe to redirect to, else raise."""
    url = url.strip()
    if not url:
        raise _invalid_url("url must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise _invalid_url(f"url must be at most {MAX_URL_LENGTH} characters")
    # Whitespace, control characters and backslashes make parsers disagree
    # about where the host ends, which is a classic SSRF trick.
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == "\\" for ch in url):
        raise _invalid_url("url contains whitespace, control characters or backslashes")

    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError when the port is invalid
    except ValueError:
        raise _invalid_url("url is not a valid URL") from None

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise _invalid_url("only http and https URLs are allowed")
    if not parts.netloc or not hostname:
        raise _invalid_url("url must include a host")
    if "@" in parts.netloc:
        raise _invalid_url("url must not contain credentials (user:pass@)")

    _check_host(hostname)
    return url


def _check_host(host: str) -> None:
    host = host.rstrip(".")  # "localhost." is still localhost
    if not host or not host.isascii():
        raise _invalid_url("host must be ASCII (use punycode for international names)")

    if ":" in host:  # IPv6 literal (urlsplit already removed the brackets)
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise _invalid_url("host is not a valid IPv6 address") from None
        _reject_non_public(ip)
        return

    labels = host.split(".")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        # Looks like an IPv4 address, possibly encoded: 2130706433, 0x7f000001,
        # 0177.0.0.1, 127.1 ... Browsers treat all of these as IPv4.
        ip = parse_ipv4_literal(labels)
        if ip is None:
            raise _invalid_url("host looks like an IP address but is not a valid one")
        _reject_non_public(ip)
        return

    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise _invalid_url("host is not allowed (local or internal name)")
    if "." not in host:
        raise _invalid_url("host must be a fully qualified domain name")
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise _invalid_url("host is not a valid domain name")


def parse_ipv4_literal(labels: list[str]) -> ipaddress.IPv4Address | None:
    """Parse inet_aton-style IPv4 (1-4 parts; decimal, 0x hex or 0 octal)."""
    if not 1 <= len(labels) <= 4:
        return None
    numbers = [_parse_ipv4_number(label) for label in labels]
    if any(n is None for n in numbers):
        return None
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(labels)):
        return None
    value = last
    for position, n in enumerate(head):
        value += n << (8 * (3 - position))
    return ipaddress.IPv4Address(value)


def _parse_ipv4_number(label: str) -> int | None:
    if _HEX_LABEL.fullmatch(label):
        return int(label[2:] or "0", 16)
    if _OCTAL_LABEL.fullmatch(label):
        return int(label, 8)
    if _DECIMAL_LABEL.fullmatch(label):
        return int(label)
    return None  # e.g. "08" (bad octal) or not a number at all


def _reject_non_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is really 127.0.0.1
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    ):
        raise _invalid_url("host is a private, loopback, reserved or otherwise non-public IP")


# --- alias -------------------------------------------------------------------
def validate_alias(alias: str) -> str:
    """Return the alias if it follows the rules, else raise."""
    if not ALIAS_MIN_LENGTH <= len(alias) <= ALIAS_MAX_LENGTH:
        raise ValidationFailed(
            "invalid_alias",
            f"alias must be {ALIAS_MIN_LENGTH}-{ALIAS_MAX_LENGTH} characters long",
        )
    if not ALIAS_PATTERN.fullmatch(alias):
        raise ValidationFailed(
            "invalid_alias", "alias may only contain letters, digits, '_' and '-'"
        )
    if alias.lower() in RESERVED_ALIASES:
        raise ValidationFailed("reserved_alias", f"'{alias}' is a reserved word")
    return alias


# --- ttl ---------------------------------------------------------------------
def validate_ttl(ttl_seconds: int) -> int:
    """Return the ttl if it is within bounds, else raise."""
    if not MIN_TTL_SECONDS <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ValidationFailed(
            "invalid_ttl",
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}",
        )
    return ttl_seconds
