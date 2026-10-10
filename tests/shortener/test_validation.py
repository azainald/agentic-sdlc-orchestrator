import pytest

from shortener.errors import ValidationFailed
from shortener.validation import (
    MAX_TTL_SECONDS,
    MAX_URL_LENGTH,
    validate_alias,
    validate_destination,
    validate_ttl,
)

ALLOWED_URLS = [
    "https://example.com",
    "http://example.com/path?query=1#frag",
    "HTTPS://Example.COM/Upper",
    "https://sub.domain.example.co.uk:8443/a/b",
    "https://8.8.8.8/dns",
    "http://1.1.1.1",
    "https://[2606:4700:4700::1111]/",
    "https://xn--bcher-kva.example/",
    "https://example.com./trailing-dot",
    "https://my-site_1.example.org/",
    "https://internal.example.com/",  # 'internal' only blocked as a suffix
    "https://localhost.example.com/",  # only the exact name / .localhost is blocked
]

BLOCKED_URLS = [
    # schemes
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "ftp://example.com/file",
    "gopher://example.com/",
    "//example.com/no-scheme",
    "example.com/no-scheme",
    "http:example.com",
    "http:///path-without-host",
    # userinfo
    "http://user@example.com/",
    "http://user:pass@example.com/",
    "http://example.com@evil.com/",
    "http://@example.com/",
    # localhost and internal names
    "http://localhost/",
    "http://LOCALHOST:8080/",
    "http://localhost./",
    "http://foo.localhost/",
    "http://db.internal/",
    "http://printer.local/",
    "http://intranet/",
    # IPv4 private / loopback / link-local / reserved / multicast
    "http://127.0.0.1/",
    "http://127.255.255.254/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",
    "http://224.0.0.1/",
    "http://240.0.0.1/",
    "http://255.255.255.255/",
    # IPv6
    "http://[::1]/",
    "http://[::]/",
    "http://[fe80::1]/",
    "http://[fc00::1]/",
    "http://[fd12:3456::1]/",
    "http://[ff02::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[not-an-ip]/",
    # encoded IPv4
    "http://2130706433/",  # decimal 127.0.0.1
    "http://0x7f000001/",  # hex 127.0.0.1
    "http://0x7f.0.0.1/",
    "http://0177.0.0.1/",  # octal
    "http://017700000001/",  # octal single number
    "http://127.1/",  # short form
    "http://127.0.1/",
    "http://167772161/",  # decimal 10.0.0.1
    "http://3232235777/",  # decimal 192.168.1.1
    "http://2852039166/",  # decimal 169.254.169.254
    # numeric hosts that are not valid IPs at all
    "http://999999999999/",
    "http://256.256.256.256/",
    "http://1.2.3.4.5/",
    "http://08.0.0.1/",
    # sneaky formatting
    "http://example.com\\@127.0.0.1/",
    "http://exa mple.com/",
    "http://example.com/\nHost: evil",
    "http://example.com:notaport/",
    "http://example.com:99999/",
    "http://１２７.0.0.1/",  # fullwidth digits
    "http://bücher.example/",  # non-ASCII (needs punycode)
    "",
    "   ",
]


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_allowed_destinations(url):
    assert validate_destination(url) == url


@pytest.mark.parametrize("url", BLOCKED_URLS)
def test_blocked_destinations(url):
    with pytest.raises(ValidationFailed) as error:
        validate_destination(url)
    assert error.value.code == "invalid_url"


def test_destination_is_stripped():
    assert validate_destination("  https://example.com/  ") == "https://example.com/"


def test_destination_length_limit():
    prefix = "https://example.com/"
    assert validate_destination(prefix + "a" * (MAX_URL_LENGTH - len(prefix)))
    with pytest.raises(ValidationFailed):
        validate_destination(prefix + "a" * (MAX_URL_LENGTH - len(prefix) + 1))


@pytest.mark.parametrize("alias", ["abc", "My_Alias-1", "a" * 32, "ABC", "---", "a_b"])
def test_valid_aliases(alias):
    assert validate_alias(alias) == alias


@pytest.mark.parametrize(
    "alias, code",
    [
        ("ab", "invalid_alias"),  # too short
        ("a" * 33, "invalid_alias"),  # too long
        ("", "invalid_alias"),
        ("has space", "invalid_alias"),
        ("dots.not.ok", "invalid_alias"),
        ("slash/no", "invalid_alias"),
        ("caféx", "invalid_alias"),  # non-ASCII
        ("abc\n", "invalid_alias"),  # trailing newline must not slip through
        ("api", "reserved_alias"),
        ("admin", "reserved_alias"),
        ("health", "reserved_alias"),
        ("docs", "reserved_alias"),
        ("API", "reserved_alias"),  # reserved words are case-insensitive
        ("Docs", "reserved_alias"),
        ("redoc", "reserved_alias"),
    ],
)
def test_invalid_aliases(alias, code):
    with pytest.raises(ValidationFailed) as error:
        validate_alias(alias)
    assert error.value.code == code


def test_openapi_json_is_reserved_even_though_dots_are_invalid():
    with pytest.raises(ValidationFailed):
        validate_alias("openapi.json")


@pytest.mark.parametrize("ttl", [1, 60, MAX_TTL_SECONDS])
def test_valid_ttl(ttl):
    assert validate_ttl(ttl) == ttl


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1])
def test_invalid_ttl(ttl):
    with pytest.raises(ValidationFailed) as error:
        validate_ttl(ttl)
    assert error.value.code == "invalid_ttl"
