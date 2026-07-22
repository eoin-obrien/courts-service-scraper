import httpx

from courts_scraper.progress import QuietReporter
from courts_scraper.recovery import OutageBreaker, Outcome


class _FakeFetcher:
    """Fake with a controllable liveness probe."""

    def __init__(self, up_on_probe: int = 1) -> None:
        self.up_on_probe = up_on_probe  # probe number that first reports "up"
        self.probes = 0

    def is_up(self, url: str) -> bool:
        self.probes += 1
        return self.probes >= self.up_on_probe


class _FailThenSucceed:
    """Raise ReadTimeout for the first N calls, then succeed."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ReadTimeout("down")


def _raise_timeout() -> None:
    raise httpx.ReadTimeout("down")


def _breaker(fetcher, sleeps, **kwargs) -> OutageBreaker:
    return OutageBreaker(
        fetcher, "https://x/", QuietReporter(), sleep=sleeps.append, **kwargs
    )


def test_success_is_ok():
    outcome, exc = _breaker(_FakeFetcher(), []).run(lambda: None)
    assert outcome is Outcome.OK
    assert exc is None


def test_isolated_failure_defers():
    outcome, exc = _breaker(_FakeFetcher(), [], threshold=3).run(_raise_timeout)
    assert outcome is Outcome.DEFER
    assert isinstance(exc, httpx.ReadTimeout)


def test_consecutive_resets_on_success():
    breaker = _breaker(_FakeFetcher(), [], threshold=2)
    assert breaker.run(_raise_timeout)[0] is Outcome.DEFER  # consecutive = 1
    assert breaker.run(lambda: None)[0] is Outcome.OK  # reset to 0
    # A later isolated failure must not immediately trip the outage threshold.
    assert breaker.run(_raise_timeout)[0] is Outcome.DEFER  # consecutive = 1 again


def test_outage_pauses_probes_and_resumes():
    sleeps: list[float] = []
    fetcher = _FakeFetcher(up_on_probe=2)  # site comes back on the 2nd probe
    breaker = _breaker(
        fetcher, sleeps, threshold=3, intervals=(1.0, 2.0), max_outage=100.0
    )

    assert breaker.run(_raise_timeout)[0] is Outcome.DEFER  # 1
    assert breaker.run(_raise_timeout)[0] is Outcome.DEFER  # 2

    action = _FailThenSucceed(fail_times=1)  # fails, triggering the pause, then works
    outcome, _ = breaker.run(action)  # 3rd consecutive -> pause -> probe -> retry

    assert outcome is Outcome.OK
    assert action.calls == 2  # failed once, retried once after recovery
    assert fetcher.probes == 2  # probed twice (up on the second)
    assert sleeps == [1.0, 2.0]  # escalating waits before each probe


def test_gives_up_after_outage_cap():
    sleeps: list[float] = []
    fetcher = _FakeFetcher(up_on_probe=999)  # never recovers
    breaker = _breaker(fetcher, sleeps, threshold=1, intervals=(10.0,), max_outage=25.0)

    outcome, exc = breaker.run(_raise_timeout)

    assert outcome is Outcome.GAVE_UP
    assert isinstance(exc, httpx.ReadTimeout)
    assert sum(sleeps) == 25.0  # waited up to the cap, then stopped
