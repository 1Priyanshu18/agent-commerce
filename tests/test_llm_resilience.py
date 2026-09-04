import pytest

from agent_commerce.core.llm.fake import text_response
from agent_commerce.core.llm.resilience import (
    CallBudgetExceededError,
    FatalError,
    GuardedLLMClient,
    RateLimiter,
    RetryableError,
)
from agent_commerce.core.llm.types import Message


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _RecordingSleep:
    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.now += seconds


# --- RateLimiter ---


def test_rate_limiter_allows_up_to_the_limit_without_sleeping() -> None:
    clock = _FakeClock()
    sleep = _RecordingSleep(clock)
    limiter = RateLimiter(3, clock=clock, sleep=sleep)
    for _ in range(3):
        limiter.acquire()
    assert sleep.calls == []


def test_rate_limiter_sleeps_when_over_the_limit_within_the_window() -> None:
    clock = _FakeClock()
    sleep = _RecordingSleep(clock)
    limiter = RateLimiter(2, clock=clock, sleep=sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # third call within the same 60s window must wait
    assert len(sleep.calls) == 1
    assert sleep.calls[0] > 0


def test_rate_limiter_does_not_sleep_once_the_window_has_passed() -> None:
    clock = _FakeClock()
    sleep = _RecordingSleep(clock)
    limiter = RateLimiter(1, clock=clock, sleep=sleep)
    limiter.acquire()
    clock.now += 61  # window fully expired
    limiter.acquire()
    assert sleep.calls == []


# --- GuardedLLMClient: retry/backoff ---


class _ScriptedClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, actions: list) -> None:
        self._actions = list(actions)
        self.attempts = 0

    def complete(self, **kwargs):
        self.attempts += 1
        action = self._actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _no_sleep(_seconds: float) -> None:
    pass


def test_retries_on_retryable_error_and_eventually_succeeds() -> None:
    wrapped = _ScriptedClient([RetryableError("429"), RetryableError("429"), text_response("ok")])
    guarded = GuardedLLMClient(
        wrapped, requests_per_minute=1000, max_calls_per_run=100, sleep=_no_sleep, rand=lambda: 0.0
    )
    response = guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    assert response.text == "ok"
    assert wrapped.attempts == 3


def test_fatal_error_is_not_retried() -> None:
    wrapped = _ScriptedClient([FatalError("bad request"), text_response("should never be reached")])
    guarded = GuardedLLMClient(
        wrapped, requests_per_minute=1000, max_calls_per_run=100, sleep=_no_sleep, rand=lambda: 0.0
    )
    with pytest.raises(FatalError):
        guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    assert wrapped.attempts == 1


def test_gives_up_after_max_retries() -> None:
    wrapped = _ScriptedClient([RetryableError("429")] * 10)
    guarded = GuardedLLMClient(
        wrapped,
        requests_per_minute=1000,
        max_calls_per_run=100,
        max_retries=2,
        sleep=_no_sleep,
        rand=lambda: 0.0,
    )
    with pytest.raises(RetryableError):
        guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    assert wrapped.attempts == 3  # initial + 2 retries


def test_backoff_delay_grows_between_retries() -> None:
    wrapped = _ScriptedClient([RetryableError("429"), RetryableError("429"), text_response("ok")])
    sleeps: list[float] = []
    guarded = GuardedLLMClient(
        wrapped,
        requests_per_minute=1000,
        max_calls_per_run=100,
        base_delay=1.0,
        sleep=lambda s: sleeps.append(s),
        rand=lambda: 0.0,
    )
    guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    assert sleeps == [1.0, 2.0]  # base_delay * 2**attempt, jitter=0


# --- GuardedLLMClient: call budget ---


def test_call_budget_exceeded_raises_and_names_provider_and_limit() -> None:
    wrapped = _ScriptedClient([text_response("a"), text_response("b")])
    guarded = GuardedLLMClient(wrapped, requests_per_minute=1000, max_calls_per_run=1, sleep=_no_sleep)
    guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    with pytest.raises(CallBudgetExceededError, match="fake.*1"):
        guarded.complete(system="sys", messages=[Message(role="user", content="hi")])


def test_calls_made_is_tracked() -> None:
    wrapped = _ScriptedClient([text_response("a"), text_response("b")])
    guarded = GuardedLLMClient(wrapped, requests_per_minute=1000, max_calls_per_run=10, sleep=_no_sleep)
    guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    guarded.complete(system="sys", messages=[Message(role="user", content="hi")])
    assert guarded.calls_made == 2
