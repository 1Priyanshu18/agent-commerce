"""Rate limiting, retry/backoff, and a per-run call budget — provider-agnostic wrapping
applied identically regardless of which adapter is underneath. Free tiers have hard daily
caps and the eval grid is thousands of calls, so this exists from the start, not bolted on
in Phase 8.
"""

from __future__ import annotations

import random
import time
from collections import deque
from typing import Any

from .types import LLMResponse


class RetryableError(Exception):
    """A transient failure (429, 5xx, timeout, connection error) — safe to retry."""


class FatalError(Exception):
    """A non-retryable failure (auth failure, malformed request) — never retried."""


class CallBudgetExceededError(Exception):
    def __init__(self, provider: str, limit: int) -> None:
        super().__init__(f"{provider}: per-run call budget of {limit} calls exceeded")
        self.provider = provider
        self.limit = limit


class RateLimiter:
    """Sliding-window requests-per-minute limiter. Blocks (sleeps) rather than rejecting."""

    def __init__(
        self, requests_per_minute: int, *, clock: Any = time.monotonic, sleep: Any = time.sleep
    ) -> None:
        self._rpm = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._timestamps: deque[float] = deque()

    def _evict_expired(self, now: float) -> None:
        window_start = now - 60.0
        while self._timestamps and self._timestamps[0] < window_start:
            self._timestamps.popleft()

    def acquire(self) -> None:
        now = self._clock()
        self._evict_expired(now)
        if len(self._timestamps) >= self._rpm:
            wait = 60.0 - (now - self._timestamps[0])
            if wait > 0:
                self._sleep(wait)
            self._evict_expired(self._clock())
        self._timestamps.append(self._clock())


class GuardedLLMClient:
    """Wraps a raw provider adapter with rate limiting, retry/backoff on RetryableError, and
    a hard per-run call budget. FatalError is never retried. On budget exhaustion this raises
    CallBudgetExceededError naming the provider and the limit — it never silently degrades or
    falls through to a different provider.
    """

    def __init__(
        self,
        wrapped: Any,
        *,
        requests_per_minute: int,
        max_calls_per_run: int,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        sleep: Any = time.sleep,
        rand: Any = random.random,
    ) -> None:
        self._wrapped = wrapped
        self._limiter = RateLimiter(requests_per_minute, sleep=sleep)
        self._max_calls_per_run = max_calls_per_run
        self._calls_made = 0
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._rand = rand

    @property
    def provider(self) -> str:
        return self._wrapped.provider

    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def calls_made(self) -> int:
        return self._calls_made

    def complete(self, **kwargs: Any) -> LLMResponse:
        if self._calls_made >= self._max_calls_per_run:
            raise CallBudgetExceededError(self._wrapped.provider, self._max_calls_per_run)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._limiter.acquire()
            self._calls_made += 1
            try:
                return self._wrapped.complete(**kwargs)
            except RetryableError as e:
                last_error = e
                if attempt >= self._max_retries:
                    break
                delay = min(self._base_delay * (2**attempt) + self._rand(), self._max_delay)
                self._sleep(delay)
        assert last_error is not None
        raise last_error
