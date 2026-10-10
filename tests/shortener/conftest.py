import pytest
from fastapi.testclient import TestClient

from shortener.app import create_app
from shortener.ratelimit import RateLimiter
from shortener.store import UrlStore


class FakeClock:
    """A clock the test moves by hand. Starts at 2030-01-01T00:00:00Z."""

    def __init__(self, start: float = 1_893_456_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock) -> UrlStore:
    return UrlStore(clock=clock)


@pytest.fixture
def app(store, clock):
    return create_app(
        store=store, limiter=RateLimiter(clock=clock), clock=clock, ip_salt="test-salt"
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)
