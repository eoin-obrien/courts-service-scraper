"""A simple politeness gate: enforce a minimum spacing between requests.

The scraper targets a public government server, so requests are serialised and
spaced by a configurable base delay plus a small random jitter (to avoid a
perfectly periodic request pattern). Clock, sleep and RNG are injectable so the
behaviour is deterministically testable without real time passing.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from courts_scraper.progress import (
    ProgressReporter,
    QuietReporter,
    WaitReason,
    WaitStarted,
)


class RateLimiter:
    """Blocks until at least ``delay`` (+ jitter) seconds since the last call."""

    def __init__(
        self,
        delay: float,
        jitter: float = 0.0,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        """Initialise the limiter.

        Args:
            delay: Minimum seconds between the start of consecutive requests.
            jitter: Maximum extra random seconds added to each delay.
            sleep: Sleep function (injectable for tests).
            monotonic: Monotonic clock (injectable for tests).
            rng: Random source (injectable for deterministic tests).
        """
        self._delay = max(0.0, delay)
        self._jitter = max(0.0, jitter)
        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()
        self._last: float | None = None
        self._reporter: ProgressReporter = QuietReporter()

    def set_reporter(self, reporter: ProgressReporter) -> None:
        """Attach the progress reporter that receives politeness waits."""
        self._reporter = reporter

    def wait(self, *, announce: bool = True) -> None:
        """Sleep as needed so successive calls are adequately spaced.

        Emits a :class:`WaitStarted` before a real (non-zero) sleep so a dashboard
        can animate a countdown while this call blocks -- but only when there is
        actually time to wait and this is not the first request, and only when
        ``announce`` is set (an outage liveness probe passes ``announce=False`` so
        its wait is not mislabelled as politeness).
        """
        target = self._delay + self._rng.uniform(0.0, self._jitter)
        now = self._monotonic()
        if self._last is not None:
            remaining = target - (now - self._last)
            if remaining > 0:
                if announce:
                    # The wait ends ``remaining`` seconds from ``now`` (which we
                    # already read); do not consume another clock reading.
                    self._reporter.emit(
                        WaitStarted(WaitReason.POLITENESS, remaining, now + remaining)
                    )
                self._sleep(remaining)
        self._last = self._monotonic()
