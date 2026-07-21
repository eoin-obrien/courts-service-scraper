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
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum, auto

import httpx
from rich.console import Console

from courts_scraper.http import Fetcher

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
        console: Console,
        *,
        threshold: int = OUTAGE_THRESHOLD,
        intervals: tuple[float, ...] = PROBE_INTERVALS,
        max_outage: float = MAX_OUTAGE_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure the breaker (clock/sleep injectable for tests)."""
        self._fetcher = fetcher
        self._probe_url = probe_url
        self._console = console
        self._threshold = threshold
        self._intervals = intervals
        self._max_outage = max_outage
        self._sleep = sleep
        self._consecutive = 0

    def run(self, action: Callable[[], None]) -> tuple[Outcome, Exception | None]:
        """Run ``action`` with outage handling.

        Non-HTTP exceptions (parse errors, cancellation) propagate to the caller;
        only :class:`httpx.HTTPError` feeds the outage logic.

        Returns:
            ``(OK, None)`` on success; ``(DEFER, exc)`` for an isolated failure;
            ``(GAVE_UP, exc)`` if the site stayed down past the cap.
        """
        while True:
            try:
                action()
            except httpx.HTTPError as exc:
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
        self._console.print(
            "[yellow]Site appears down. Pausing until it recovers...[/]"
        )
        waited = 0.0
        step = 0
        while waited < self._max_outage:
            interval = self._intervals[min(step, len(self._intervals) - 1)]
            interval = min(interval, self._max_outage - waited)
            self._console.print(f"  probing again in {int(interval)}s...")
            self._sleep(interval)
            waited += interval
            step += 1
            if self._fetcher.is_up(self._probe_url):
                self._console.print(
                    f"[green]Site is back after ~{int(waited)}s. Resuming.[/]"
                )
                return True
        self._console.print(
            "[red]Site still down after the outage cap; stopping. "
            "Resume later with `courts-scraper download --latest`.[/]"
        )
        return False
