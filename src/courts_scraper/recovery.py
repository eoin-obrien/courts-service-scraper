"""Outage handling: pause and wait for the site to recover during a long outage.

The Courts Service site occasionally goes down for tens of minutes (e.g. while
new documents are uploaded). A single slow page should be skipped and retried
later, but a genuine outage should *not* cause the scraper to race through the
remaining work failing every request against a server that is down.

:class:`OutageBreaker` distinguishes the two by counting *consecutive* failures.
Below the threshold, a failure is treated as an isolated bad page (the caller
defers it and moves on). At the threshold, the site is assumed down: the crawl
pauses, re-probes on an escalating interval until the server responds, then
retries the same item. If the outage outlasts a cap, it gives up so the run can
be resumed later rather than hanging forever.

A failure counts toward the breaker when it is one of ``outage_errors``. That
defaults to :class:`httpx.HTTPError` (connection failures, timeouts, dropped
sockets, 4xx/5xx), but the download/revalidate phases widen it to also include
:class:`~courts_scraper.download.DownloadIncomplete`. That covers the outage
flavour :class:`httpx.HTTPError` misses: a server that stays *up* at the HTTP
layer during scheduled maintenance and answers every request with ``200`` and a
non-PDF holding page. Without it, each such response is an isolated per-item
error the breaker never sees, so the crawl races through every remaining file
failing it on first attempt instead of pausing. Anything not in ``outage_errors``
(an unsafe filename, a DB error) still propagates to the caller unchanged, so a
genuine per-item bug is never mistaken for an outage.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum, auto

import httpx

from courts_scraper.http import Fetcher
from courts_scraper.progress import (
    OutageGaveUp,
    OutagePaused,
    OutageProbing,
    OutageRecovered,
    ProgressReporter,
    WaitReason,
    WaitStarted,
)

#: Consecutive failures before the site is assumed to be down.
OUTAGE_THRESHOLD = 3
#: Escalating seconds to wait between recovery probes; the last value repeats.
PROBE_INTERVALS = (60.0, 120.0, 300.0)
#: Give up (and let the user resume later) after this much continuous downtime.
MAX_OUTAGE_SECONDS = 3600.0


class Outcome(Enum):
    """Result of running one item through the breaker."""

    OK = auto()  # succeeded
    DEFER = auto()  # isolated failure -- caller should record/skip and continue
    GAVE_UP = auto()  # site stayed down past the cap -- caller should stop


class OutageBreaker:
    """Runs work items, pausing the whole crawl while the site is down."""

    def __init__(
        self,
        fetcher: Fetcher,
        probe_url: str,
        reporter: ProgressReporter,
        *,
        threshold: int = OUTAGE_THRESHOLD,
        intervals: tuple[float, ...] = PROBE_INTERVALS,
        max_outage: float = MAX_OUTAGE_SECONDS,
        outage_errors: tuple[type[Exception], ...] = (httpx.HTTPError,),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the breaker (clock/sleep injectable for tests).

        ``outage_errors`` are the exception types a failed ``action`` raises that
        should count toward the consecutive-failure tally; anything else
        propagates to the caller. Defaults to :class:`httpx.HTTPError`; download
        phases add :class:`~courts_scraper.download.DownloadIncomplete` so a run of
        ``200`` maintenance-page responses trips the same pause.
        """
        self._fetcher = fetcher
        self._probe_url = probe_url
        self._reporter = reporter
        self._threshold = threshold
        self._intervals = intervals
        self._max_outage = max_outage
        self._outage_errors = outage_errors
        self._sleep = sleep
        self._monotonic = monotonic
        self._consecutive = 0

    def run(self, action: Callable[[], None]) -> tuple[Outcome, Exception | None]:
        """Run ``action`` with outage handling.

        Exceptions outside ``outage_errors`` (parse errors, cancellation, an
        unsafe filename) propagate to the caller; only an ``outage_errors``
        failure feeds the outage logic.

        Returns:
            ``(OK, None)`` on success; ``(DEFER, exc)`` for an isolated failure;
            ``(GAVE_UP, exc)`` if the site stayed down past the cap.
        """
        while True:
            try:
                action()
            except self._outage_errors as exc:
                self._consecutive += 1
                if self._consecutive < self._threshold:
                    return Outcome.DEFER, exc
                if self._wait_for_recovery():
                    self._consecutive = 0
                    continue  # site is back -- retry the same item
                return Outcome.GAVE_UP, exc
            else:
                self._consecutive = 0
                return Outcome.OK, None

    def _wait_for_recovery(self) -> bool:
        """Sleep and probe until the site responds, or the cap is exceeded."""
        self._reporter.emit(OutagePaused(self._consecutive))
        waited = 0.0
        step = 0
        while waited < self._max_outage:
            interval = self._intervals[min(step, len(self._intervals) - 1)]
            interval = min(interval, self._max_outage - waited)
            self._reporter.emit(OutageProbing(waited, interval))
            self._reporter.emit(
                WaitStarted(
                    WaitReason.OUTAGE_PROBE, interval, self._monotonic() + interval
                )
            )
            self._sleep(interval)
            waited += interval
            step += 1
            if self._fetcher.is_up(self._probe_url):
                self._reporter.emit(OutageRecovered(waited))
                return True
        self._reporter.emit(OutageGaveUp(waited))
        return False
