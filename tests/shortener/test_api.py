import re

import pytest
from fastapi.testclient import TestClient

from shortener.app import create_app
from shortener.ratelimit import RateLimiter
from shortener.service import BASE62_ALPHABET, CODE_LENGTH, UrlService
from shortener.store import UrlStore
from tests.shortener.helpers import create_link

URLS = "/api/v1/urls"


def get_no_follow(client, path, **kwargs):
    return client.get(path, follow_redirects=False, **kwargs)


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]


# --- health ------------------------------------------------------------------
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- create ------------------------------------------------------------------
def test_create_returns_201_with_documented_fields(client, clock):
    response = client.post(URLS, json={"url": "https://example.com/a", "ttl_seconds": 3600})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"code", "short_url", "url", "expires_at", "created_at"}
    assert re.fullmatch(f"[{BASE62_ALPHABET}]{{{CODE_LENGTH}}}", body["code"])
    assert body["short_url"] == f"http://testserver/{body['code']}"
    assert body["url"] == "https://example.com/a"
    assert body["created_at"] == "2030-01-01T00:00:00Z"
    assert body["expires_at"] == "2030-01-01T01:00:00Z"


def test_create_without_ttl_never_expires(client):
    assert create_link(client)["expires_at"] is None


def test_create_with_custom_alias(client):
    body = create_link(client, alias="my-link_1")
    assert body["code"] == "my-link_1"
    assert get_no_follow(client, "/my-link_1").status_code == 302


def test_duplicate_alias_returns_409(client):
    create_link(client, alias="taken")
    response = client.post(URLS, json={"url": "https://other.example", "alias": "taken"})
    assert_error(response, 409, "alias_taken")


@pytest.mark.parametrize("alias", ["api", "admin", "health", "docs", "ab", "bad alias!"])
def test_bad_alias_returns_422(client, alias):
    response = client.post(URLS, json={"url": "https://example.com", "alias": alias})
    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"invalid_alias", "reserved_alias"}


@pytest.mark.parametrize("ttl", [0, -5, 31_536_001, "soon"])
def test_bad_ttl_returns_422(client, ttl):
    response = client.post(URLS, json={"url": "https://example.com", "ttl_seconds": ttl})
    assert_error(response, 422, "validation_error")


@pytest.mark.parametrize("ttl", [1, 31_536_000])
def test_ttl_bounds_are_inclusive(client, ttl):
    assert client.post(URLS, json={"url": "https://example.com", "ttl_seconds": ttl}).status_code == 201


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "http://localhost/", "http://2130706433/", "http://u:p@a.com/"]
)
def test_dangerous_destination_returns_422(client, url):
    assert_error(client.post(URLS, json={"url": url}), 422, "invalid_url")


def test_missing_or_unknown_body_fields_return_422(client):
    assert_error(client.post(URLS, json={}), 422, "validation_error")
    assert_error(
        client.post(URLS, json={"url": "https://example.com", "extra": 1}), 422, "validation_error"
    )


def test_random_codes_are_unique(client):
    codes = {create_link(client)["code"] for _ in range(20)}
    assert len(codes) == 20


def test_random_code_collision_is_retried(store, clock):
    codes = iter(["AAAAAAA", "AAAAAAA", "BBBBBBB"])
    service = UrlService(store, clock=clock, code_generator=lambda: next(codes))
    assert service.create("https://example.com").record.code == "AAAAAAA"
    assert service.create("https://example.org").record.code == "BBBBBBB"  # skipped the clash


def test_code_generation_gives_up_with_503(store, clock):
    service = UrlService(store, clock=clock, code_generator=lambda: "SAMECODE")
    service.create("https://example.com")
    app = create_app(store=store, clock=clock)
    app.state.service = service
    response = TestClient(app).post(URLS, json={"url": "https://example.org"})
    assert_error(response, 503, "code_generation_failed")


# --- idempotency ---------------------------------------------------------------
def test_idempotency_same_key_same_body_returns_same_payload(client, clock):
    headers = {"Idempotency-Key": "abc-123"}
    body = {"url": "https://example.com", "ttl_seconds": 60}
    first = client.post(URLS, json=body, headers=headers)
    clock.advance(5)
    second = client.post(URLS, json=body, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()


def test_idempotency_does_not_create_a_second_link(client, store):
    headers = {"Idempotency-Key": "k"}
    first = client.post(URLS, json={"url": "https://example.com"}, headers=headers).json()
    client.post(URLS, json={"url": "https://example.com"}, headers=headers)
    assert store.get(first["code"]) is not None
    assert len(store._urls) == 1


def test_idempotency_same_key_different_body_returns_409(client):
    headers = {"Idempotency-Key": "k"}
    client.post(URLS, json={"url": "https://example.com"}, headers=headers)
    response = client.post(URLS, json={"url": "https://different.example"}, headers=headers)
    assert_error(response, 409, "idempotency_key_conflict")


def test_idempotency_replay_after_delete_returns_original_payload(client):
    headers = {"Idempotency-Key": "k"}
    body = {"url": "https://example.com", "alias": "keepme"}
    first = client.post(URLS, json=body, headers=headers)
    client.delete(f"{URLS}/keepme")
    second = client.post(URLS, json=body, headers=headers)
    assert second.status_code == 200 and second.json() == first.json()


def test_different_keys_create_different_links(client):
    body = {"url": "https://example.com"}
    first = client.post(URLS, json=body, headers={"Idempotency-Key": "one"}).json()
    second = client.post(URLS, json=body, headers={"Idempotency-Key": "two"}).json()
    assert first["code"] != second["code"]


def test_idempotency_key_too_long_returns_422(client):
    response = client.post(URLS, json={"url": "https://example.com"}, headers={"Idempotency-Key": "k" * 256})
    assert_error(response, 422, "invalid_idempotency_key")


# --- redirect -------------------------------------------------------------------
def test_redirect_returns_302_with_location(client):
    code = create_link(client, url="https://example.com/target?x=1")["code"]
    response = get_no_follow(client, f"/{code}")
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/target?x=1"


def test_redirect_unknown_code_returns_404(client):
    assert_error(get_no_follow(client, "/doesnotexist"), 404, "not_found")


def test_redirect_expired_link_returns_410(client, clock):
    code = create_link(client, ttl_seconds=60)["code"]
    clock.advance(59)
    assert get_no_follow(client, f"/{code}").status_code == 302
    clock.advance(1)
    assert_error(get_no_follow(client, f"/{code}"), 410, "expired")


def test_expired_link_does_not_record_clicks(client, clock):
    code = create_link(client, ttl_seconds=10)["code"]
    clock.advance(10)
    get_no_follow(client, f"/{code}")
    clock.advance(-10)  # look at the analytics while it is still valid
    assert client.get(f"{URLS}/{code}/analytics").json()["total_clicks"] == 0


def test_health_route_is_not_shadowed_by_redirect(client):
    assert client.get("/health").json() == {"status": "ok"}


# --- metadata / patch / delete ----------------------------------------------------
def test_get_metadata(client):
    created = create_link(client, ttl_seconds=100)
    response = client.get(f"{URLS}/{created['code']}")
    assert response.status_code == 200
    assert response.json() == {**created, "status": "active"}


def test_get_metadata_unknown_returns_404(client):
    assert_error(client.get(f"{URLS}/nope"), 404, "not_found")


def test_metadata_of_expired_link_reports_status_expired(client, clock):
    code = create_link(client, ttl_seconds=5)["code"]
    clock.advance(6)
    response = client.get(f"{URLS}/{code}")
    assert response.status_code == 200
    assert response.json()["status"] == "expired"


def test_patch_url(client):
    code = create_link(client)["code"]
    response = client.patch(f"{URLS}/{code}", json={"url": "https://new.example/x"})
    assert response.status_code == 200
    assert response.json()["url"] == "https://new.example/x"
    assert get_no_follow(client, f"/{code}").headers["location"] == "https://new.example/x"


def test_patch_ttl_counts_from_now_and_can_revive_expired_link(client, clock):
    code = create_link(client, ttl_seconds=10)["code"]
    clock.advance(20)
    response = client.patch(f"{URLS}/{code}", json={"ttl_seconds": 100})
    assert response.json()["expires_at"] == "2030-01-01T00:02:00Z"
    assert response.json()["status"] == "active"
    assert get_no_follow(client, f"/{code}").status_code == 302


def test_patch_ttl_null_removes_expiry(client):
    code = create_link(client, ttl_seconds=10)["code"]
    response = client.patch(f"{URLS}/{code}", json={"ttl_seconds": None})
    assert response.json()["expires_at"] is None


def test_patch_validates_destination(client):
    code = create_link(client)["code"]
    assert_error(client.patch(f"{URLS}/{code}", json={"url": "http://127.0.0.1/"}), 422, "invalid_url")
    assert_error(client.patch(f"{URLS}/{code}", json={"url": None}), 422, "invalid_url")


def test_patch_empty_body_returns_422(client):
    code = create_link(client)["code"]
    assert_error(client.patch(f"{URLS}/{code}", json={}), 422, "invalid_update")


def test_patch_bad_ttl_returns_422(client):
    code = create_link(client)["code"]
    assert_error(client.patch(f"{URLS}/{code}", json={"ttl_seconds": 0}), 422, "validation_error")


def test_patch_unknown_returns_404_and_deleted_returns_410(client):
    assert_error(client.patch(f"{URLS}/nope", json={"url": "https://a.example"}), 404, "not_found")
    code = create_link(client)["code"]
    client.delete(f"{URLS}/{code}")
    assert_error(client.patch(f"{URLS}/{code}", json={"url": "https://a.example"}), 410, "deleted")


def test_delete_is_soft_and_returns_204(client, store):
    code = create_link(client)["code"]
    response = client.delete(f"{URLS}/{code}")
    assert response.status_code == 204
    assert response.content == b""
    assert store.get(code).deleted_at is not None  # record is kept, only marked


def test_deleted_link_returns_410_everywhere(client):
    code = create_link(client)["code"]
    client.delete(f"{URLS}/{code}")
    assert_error(get_no_follow(client, f"/{code}"), 410, "deleted")
    assert_error(client.get(f"{URLS}/{code}"), 410, "deleted")
    assert_error(client.get(f"{URLS}/{code}/analytics"), 410, "deleted")
    assert_error(client.delete(f"{URLS}/{code}"), 410, "deleted")


def test_delete_unknown_returns_404(client):
    assert_error(client.delete(f"{URLS}/nope"), 404, "not_found")


def test_deleted_alias_cannot_be_reused(client):
    create_link(client, alias="gone-link")
    client.delete(f"{URLS}/gone-link")
    response = client.post(URLS, json={"url": "https://example.com", "alias": "gone-link"})
    assert_error(response, 409, "alias_taken")


# --- analytics ------------------------------------------------------------------------
def test_analytics_empty(client):
    code = create_link(client)["code"]
    assert client.get(f"{URLS}/{code}/analytics").json() == {
        "total_clicks": 0,
        "clicks_by_day": {},
        "top_referrers": [],
        "unique_visitors": 0,
    }


def test_analytics_unknown_returns_404(client):
    assert_error(client.get(f"{URLS}/nope/analytics"), 404, "not_found")


def test_analytics_aggregation(app, clock):
    client = TestClient(app)
    code = create_link(client)["code"]

    def click(referrer=None, ip_client=client):
        headers = {"Referer": referrer} if referrer else {}
        assert get_no_follow(ip_client, f"/{code}", headers=headers).status_code == 302

    click("https://news.example/a")
    click("https://news.example/a")
    click("https://blog.example/")
    click()  # direct visit: counted as a click but not as a referrer
    clock.advance(86_400)  # next UTC day
    click("https://news.example/a")

    other_visitor = TestClient(app, client=("203.0.113.9", 50000))
    click("https://blog.example/", ip_client=other_visitor)

    data = client.get(f"{URLS}/{code}/analytics").json()
    assert data["total_clicks"] == 6
    assert data["clicks_by_day"] == {"2030-01-01": 4, "2030-01-02": 2}
    assert data["top_referrers"] == [
        {"referrer": "https://news.example/a", "count": 3},
        {"referrer": "https://blog.example/", "count": 2},
    ]
    assert data["unique_visitors"] == 2  # TestClient's default address + 203.0.113.9


def test_hashed_ip_never_equals_raw_ip(app, store):
    visitor = TestClient(app, client=("198.51.100.77", 1234))
    code = create_link(visitor)["code"]
    get_no_follow(visitor, f"/{code}")
    (click,) = store.get_clicks(code)
    assert click.visitor_hash != "198.51.100.77"
    assert "198.51.100.77" not in repr(click)
    assert re.fullmatch(r"[0-9a-f]{16}", click.visitor_hash)  # truncated sha256 hex


def test_same_ip_with_different_salt_gives_different_hash(store, clock):
    clicks = []
    for salt in ("salt-one", "salt-two"):
        isolated = UrlStore(clock=clock)
        client = TestClient(create_app(store=isolated, clock=clock, ip_salt=salt))
        code = create_link(client)["code"]
        get_no_follow(client, f"/{code}")
        clicks.append(isolated.get_clicks(code)[0].visitor_hash)
    assert clicks[0] != clicks[1]


def test_analytics_never_exposes_ips(app):
    visitor = TestClient(app, client=("198.51.100.77", 1234))
    code = create_link(visitor)["code"]
    get_no_follow(visitor, f"/{code}")
    assert "198.51.100.77" not in visitor.get(f"{URLS}/{code}/analytics").text


# --- rate limiting ----------------------------------------------------------------------
def test_rate_limit_returns_429_with_retry_after(client, clock):
    for _ in range(30):
        assert client.post(URLS, json={"url": "https://example.com"}).status_code == 201
    response = client.post(URLS, json={"url": "https://example.com"})
    assert_error(response, 429, "rate_limited")
    assert response.headers["Retry-After"] == "60"
    clock.advance(30)
    assert client.post(URLS, json={"url": "https://example.com"}).headers["Retry-After"] == "30"
    clock.advance(30)
    assert client.post(URLS, json={"url": "https://example.com"}).status_code == 201


def test_rate_limit_is_per_api_key(client):
    for _ in range(30):
        client.post(URLS, json={"url": "https://example.com"}, headers={"X-API-Key": "alice"})
    blocked = client.post(URLS, json={"url": "https://example.com"}, headers={"X-API-Key": "alice"})
    other_key = client.post(URLS, json={"url": "https://example.com"}, headers={"X-API-Key": "bob"})
    assert blocked.status_code == 429
    assert other_key.status_code == 201


def test_rate_limit_falls_back_to_client_ip(app):
    first = TestClient(app, client=("192.0.2.1", 1))
    second = TestClient(app, client=("192.0.2.2", 1))
    for _ in range(30):
        first.post(URLS, json={"url": "https://example.com"})
    assert first.post(URLS, json={"url": "https://example.com"}).status_code == 429
    assert second.post(URLS, json={"url": "https://example.com"}).status_code == 201


def test_rate_limit_only_applies_to_post_create(client, clock):
    code = create_link(client)["code"]
    for _ in range(40):
        assert client.get(f"{URLS}/{code}").status_code == 200
        assert get_no_follow(client, f"/{code}").status_code == 302
    assert client.get("/health").status_code == 200


def test_custom_limiter_can_be_injected(clock):
    app = create_app(limiter=RateLimiter(limit=1, window_seconds=10, clock=clock), clock=clock)
    client = TestClient(app)
    assert client.post(URLS, json={"url": "https://example.com"}).status_code == 201
    assert client.post(URLS, json={"url": "https://example.com"}).status_code == 429


# --- app wiring / openapi -------------------------------------------------------------------
def test_apps_are_isolated_from_each_other(clock):
    first = TestClient(create_app(clock=clock))
    second = TestClient(create_app(clock=clock))
    code = create_link(first, alias="only-here")["code"]
    assert get_no_follow(first, f"/{code}").status_code == 302
    assert get_no_follow(second, f"/{code}").status_code == 404


def test_unknown_route_uses_error_format(client):
    assert_error(client.get("/a/b/c"), 404, "not_found")


def test_wrong_method_uses_error_format(client):
    assert_error(client.put(f"{URLS}/abc"), 405, "method_not_allowed")


def test_openapi_generation(app):
    schema = app.openapi()
    assert schema["info"]["title"] == "URL Shortener"
    paths = schema["paths"]
    assert set(paths[URLS]) == {"post"}
    assert set(paths[f"{URLS}/{{code}}"]) == {"get", "patch", "delete"}
    assert "/health" in paths and "/{code}" in paths
    assert "/api/v1/urls/{code}/analytics" in paths
    assert "201" in paths[URLS]["post"]["responses"]
    assert "429" in paths[URLS]["post"]["responses"]
    schemas = schema["components"]["schemas"]
    for name in ("CreateUrlRequest", "UpdateUrlRequest", "UrlResponse", "AnalyticsResponse", "ErrorResponse"):
        assert name in schemas
    ttl = schemas["CreateUrlRequest"]["properties"]["ttl_seconds"]
    assert "anyOf" in ttl  # integer or null, bounds documented on the integer branch


def test_openapi_endpoint_is_served(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["openapi"].startswith("3.")
