import random

from courts_scraper.ratelimit import RateLimiter


class FakeClock:
    """A monotonic clock driven by a fixed sequence of readings."""

    def __init__(self, readings: list[float]) -> None:
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0)


def test_first_call_does_not_sleep():
    slept: list[float] = []
    limiter = RateLimiter(
        5.0,
        0.0,
        sleep=slept.append,
        monotonic=FakeClock([0.0, 0.0]),
        rng=random.Random(0),
    )
    limiter.wait()
    assert slept == []


def test_second_call_waits_remaining_time():
    slept: list[float] = []
    # readings: wait1 -> now=0, last=0 ; wait2 -> now=1, last=1
    limiter = RateLimiter(
        5.0,
        0.0,
        sleep=slept.append,
        monotonic=FakeClock([0.0, 0.0, 1.0, 1.0]),
        rng=random.Random(0),
    )
    limiter.wait()
    limiter.wait()
    assert slept == [4.0]  # 5s target minus 1s already elapsed


def test_no_sleep_when_enough_time_passed():
    slept: list[float] = []
    limiter = RateLimiter(
        1.0,
        0.0,
        sleep=slept.append,
        monotonic=FakeClock([0.0, 0.0, 10.0, 10.0]),
        rng=random.Random(0),
    )
    limiter.wait()
    limiter.wait()
    assert slept == []
