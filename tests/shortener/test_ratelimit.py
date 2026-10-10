from shortener.ratelimit import RateLimiter


def test_allows_up_to_limit_then_blocks(clock):
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    assert [limiter.check("a").allowed for _ in range(3)] == [True, True, True]
    decision = limiter.check("a")
    assert decision.allowed is False
    assert decision.retry_after == 60


def test_retry_after_counts_down_with_the_clock(clock):
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("a")
    clock.advance(45.5)
    assert limiter.check("a").retry_after == 15  # 14.5 s rounded up


def test_window_slides(clock):
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("a")  # t=0
    clock.advance(30)
    limiter.check("a")  # t=30
    assert not limiter.check("a").allowed
    clock.advance(30)  # t=60: the first hit just left the window
    assert limiter.check("a").allowed
    assert not limiter.check("a").allowed  # t=30 and t=60 hits still count


def test_keys_are_independent(clock):
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    assert limiter.check("a").allowed
    assert limiter.check("b").allowed
    assert not limiter.check("a").allowed


def test_blocked_requests_are_not_counted(clock):
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("a")
    for _ in range(5):
        limiter.check("a")
    clock.advance(60)
    assert limiter.check("a").allowed


def test_idle_keys_are_forgotten(clock):
    limiter = RateLimiter(limit=5, window_seconds=60, clock=clock)
    limiter.check("old")
    clock.advance(120)
    limiter.check("new")
    assert "old" not in limiter._hits
