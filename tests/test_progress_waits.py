"""Unit tests for the wait/backoff events (politeness guards and retry backoff)."""

from __future__ import annotations

from courts_scraper.http import retry_before_sleep
from courts_scraper.progress import (
    Event,
    RetryScheduled,
    WaitReason,
    WaitStarted,
)
from courts_scraper.ratelimit import RateLimiter


class _Rec:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class _Clock:
    def __init__(self, readings: list[float]) -> None:
        self._readings = readings

    def __call__(self) -> float:
        return self._readings.pop(0)


def _limiter(readings: list[float], rec: _Rec, *, delay: float = 5.0) -> RateLimiter:
    limiter = RateLimiter(
        delay,
        0.0,
        sleep=lambda _s: None,
        monotonic=_Clock(readings),
        rng=_FixedRng(),
    )
    limiter.set_reporter(rec)
    return limiter


class _FixedRng:
    def uniform(self, _a: float, _b: float) -> float:
        return 0.0


def test_first_wait_is_silent():
    rec = _Rec()
    # first call reads monotonic once (no _last yet) and stamps _last.
    limiter = _limiter([1000.0, 1000.0], rec)
    limiter.wait()
    assert rec.events == []


def test_second_wait_emits_when_time_remains():
    rec = _Rec()
    # call 1: now=1000 (stamp last=1000). call 2: now=1002 -> remaining 3s.
    limiter = _limiter([1000.0, 1000.0, 1002.0, 1005.0], rec)
    limiter.wait()
    limiter.wait()
    waits = [e for e in rec.events if isinstance(e, WaitStarted)]
    assert len(waits) == 1
    assert waits[0].reason is WaitReason.POLITENESS
    assert round(waits[0].seconds, 3) == 3.0


def test_wait_announce_false_is_silent():
    rec = _Rec()
    limiter = _limiter([1000.0, 1000.0, 1002.0, 1005.0], rec)
    limiter.wait()
    limiter.wait(announce=False)  # the outage probe path
    assert [e for e in rec.events if isinstance(e, WaitStarted)] == []


def test_retry_before_sleep_emits_retry_and_backoff():
    rec = _Rec()
    before_sleep = retry_before_sleep(
        lambda: rec, lambda _state: "http://x/pdf", 4, monotonic=lambda: 500.0
    )

    class _NextAction:
        sleep = 8.0

    class _State:
        attempt_number = 2
        next_action = _NextAction()

    before_sleep(_State())
    retries = [e for e in rec.events if isinstance(e, RetryScheduled)]
    waits = [e for e in rec.events if isinstance(e, WaitStarted)]
    assert retries == [RetryScheduled("http://x/pdf", 2, 4, 8.0)]
    assert waits[0].reason is WaitReason.RETRY_BACKOFF
    assert waits[0].until_monotonic == 508.0
