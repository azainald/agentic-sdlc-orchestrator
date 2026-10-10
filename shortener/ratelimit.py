"""In-memory sliding-window rate limiter.

For each client key we remember the timestamps of its recent requests. A new
request is allowed when fewer than `limit` of them happened in the last
`window_seconds`. State is per process (not shared between servers).
"""
import math
import threading
from collections import deque
from dataclasses import dataclass

from shortener.clock import Clock, system_clock

DEFAULT_LIMIT = 30
DEFAULT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0  # whole seconds to wait; only meaningful when not allowed


class RateLimiter:
    def __init__(
        self,
        limit: int = DEFAULT_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Clock = system_clock,
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = clock()

    def check(self, key: str) -> RateLimitDecision:
        """Record a request for `key` if allowed, and say whether it is."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window_seconds:
                hits.popleft()  # forget requests that left the window
            if len(hits) >= self.limit:
                wait = hits[0] + self.window_seconds - now
                return RateLimitDecision(allowed=False, retry_after=max(1, math.ceil(wait)))
            hits.append(now)
            self._forget_idle_keys(now)
            return RateLimitDecision(allowed=True)

    def _forget_idle_keys(self, now: float) -> None:
        """Keep memory bounded: once per window, drop keys with only expired hits."""
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        idle = [k for k, h in self._hits.items() if h and h[-1] <= now - self.window_seconds]
        for key in idle:
            del self._hits[key]
