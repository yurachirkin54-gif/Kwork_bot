"""Per-domain rate limiting, jitter, exponential backoff and circuit breakers."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from app.models import as_utc


class CircuitBreaker:
    """Trips after N consecutive failures and cools down 5m -> 15m -> 1h."""

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_schedule: Sequence[int] = (300, 900, 3600),
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_schedule = list(cooldown_schedule) or [300, 900, 3600]
        self.failures = 0
        self.open_count = 0
        self.opened_until: datetime | None = None
        self.last_error: str | None = None
        self.last_state: str | None = None

    @property
    def is_open(self) -> bool:
        return self.opened_until is not None and as_utc(self.opened_until) > datetime.now(timezone.utc)

    def cooldown_remaining(self, now: datetime | None = None) -> int:
        if self.opened_until is None:
            return 0
        reference = now or datetime.now(timezone.utc)
        remaining = (as_utc(self.opened_until) - reference).total_seconds()
        return max(0, int(remaining))

    def can_attempt(self, now: datetime | None = None) -> bool:
        return self.cooldown_remaining(now) <= 0

    def record_success(self) -> None:
        self.failures = 0
        self.open_count = 0
        self.opened_until = None
        self.last_error = None
        self.last_state = None

    def record_failure(self, reason: str = "", now: datetime | None = None) -> int | None:
        """Register a failure; returns the cooldown in seconds when the breaker trips."""

        reference = now or datetime.now(timezone.utc)
        self.failures += 1
        self.last_error = reason or self.last_error
        if self.failures < self.failure_threshold:
            return None
        index = min(self.open_count, len(self.cooldown_schedule) - 1)
        cooldown = self.cooldown_schedule[index]
        self.opened_until = reference + timedelta(seconds=cooldown)
        self.open_count += 1
        self.failures = 0
        return cooldown


class RateLimiter:
    """Ensures a minimum interval between requests to the same key (host)."""

    def __init__(self, default_interval: float = 1.0, jitter: float = 0.35) -> None:
        self.default_interval = max(0.0, default_interval)
        self.jitter = max(0.0, jitter)
        self._last_call: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def acquire(self, key: str, interval: float | None = None) -> float:
        """Wait until the next allowed slot; returns the actual sleep duration."""

        target = self.default_interval if interval is None else max(0.0, interval)
        delay = 0.0
        async with self._lock_for(key):
            loop = asyncio.get_running_loop()
            now = loop.time()
            last = self._last_call.get(key, 0.0)
            wait = (last + target) - now
            if wait > 0:
                delay = wait + random.uniform(0, self.jitter * target) if self.jitter else wait
                await asyncio.sleep(delay)
            self._last_call[key] = loop.time()
        return delay

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._last_call.clear()
        else:
            self._last_call.pop(key, None)
