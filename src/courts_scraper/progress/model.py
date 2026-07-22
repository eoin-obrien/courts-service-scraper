"""The mutable run state behind every live sink, and its immutable snapshot.

The engine thread mutates a :class:`ProgressModel` (via :meth:`ProgressModel.apply`)
while a render thread reads it. To make that safe, the render thread never touches
the live object: it calls :meth:`ProgressModel.snapshot`, which copies every field
out under a lock into a frozen :class:`Snapshot`. That avoids the
``RuntimeError: dictionary changed size during iteration`` a naive shared read
would hit when the engine inserts a key mid-render.

:class:`Snapshot` also carries the *raw* ``wait_until_monotonic`` rather than a
pre-computed "seconds remaining", so the renderer can animate the countdown against
its own clock between snapshots (see the P0 spike: the refresh thread ticks while
the engine thread is blocked in ``sleep()``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from courts_scraper.progress.events import (
    Cancelled,
    Event,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    OutageGaveUp,
    OutagePaused,
    OutageProbing,
    OutageRecovered,
    PhaseStarted,
    RequestStarted,
    RetryScheduled,
    RunFinished,
    RunStarted,
    WaitReason,
    WaitStarted,
)

#: Requests observed before the ETA blends in the run's measured pace.
_ETA_BLEND_AFTER = 20


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An immutable, consistent view of a run for rendering off-thread."""

    run_name: str
    courts: tuple[str, ...]
    phase_name: str
    phase_index: int
    phase_total: int
    phase_done: int
    phase_items: int
    requests_done: int
    est_total_requests: int
    ok: int
    skipped: int
    error: int
    deferred: int
    retry: int
    current_label: str
    wait_reason: WaitReason | None
    wait_until_monotonic: float | None
    requesting: bool
    outage_state: str
    outage_down_seconds: float
    outage_probe_in: float
    cancelled: bool
    finished: bool
    incomplete: bool
    eta_seconds: float | None
    done_at_wall: float | None
    elapsed_seconds: float
    rate_per_min: float | None


class ProgressModel:
    """Thread-safe run state fed by events; read via :meth:`snapshot`."""

    def __init__(
        self,
        *,
        delay: float,
        jitter: float,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """Initialise an empty model.

        Args:
            delay: Configured politeness delay (seconds) -- the ETA floor pace.
            jitter: Configured politeness jitter (seconds).
            monotonic: Monotonic clock (injectable for tests).
            wall: Wall clock (injectable for tests).
        """
        self._lock = threading.Lock()
        self._delay = delay
        self._jitter = jitter
        self._monotonic = monotonic
        self._wall = wall

        self._run_name = ""
        self._courts: tuple[str, ...] = ()
        self._est_total_requests = 0
        self._phase_name = ""
        self._phase_index = 0
        self._phase_total = 0
        self._phase_items = 0
        self._phase_done = 0
        self._requests_done = 0
        self._ok = 0
        self._skipped = 0
        self._error = 0
        self._deferred = 0
        self._retry = 0
        self._current_label = ""
        self._wait_reason: WaitReason | None = None
        self._wait_until: float | None = None
        self._requesting = False
        self._outage_state = ""
        self._outage_down = 0.0
        self._outage_probe_in = 0.0
        self._cancelled = False
        self._finished = False
        self._incomplete = False
        self._start_monotonic: float | None = None
        self._start_wall: float | None = None
        self._done_at_frozen: float | None = None

    def apply(self, event: Event) -> None:
        """Fold one event into the run state (thread-safe)."""
        with self._lock:
            if self._start_monotonic is None:
                self._start_monotonic = self._monotonic()
                self._start_wall = self._wall()

            if isinstance(event, RunStarted):
                self._run_name = event.run_name
                self._courts = event.courts
                self._est_total_requests = event.est_total_requests
            elif isinstance(event, PhaseStarted):
                self._phase_name = event.phase
                self._phase_index = event.index
                self._phase_total = event.total
                self._phase_items = event.items
                self._phase_done = 0
                self._current_label = ""
            elif isinstance(event, ItemStarted):
                self._current_label = event.label
            elif isinstance(event, ItemFinished):
                self._phase_done += 1
                if event.status is ItemStatus.OK:
                    self._ok += 1
                elif event.status is ItemStatus.SKIPPED_NO_CITATION:
                    self._skipped += 1
                elif event.status is ItemStatus.ERROR:
                    self._error += 1
                elif event.status is ItemStatus.DEFERRED:
                    self._deferred += 1
                self._wait_reason = None
                self._wait_until = None
                self._requesting = False
            elif isinstance(event, WaitStarted):
                self._wait_reason = event.reason
                self._wait_until = event.until_monotonic
                self._requesting = False
            elif isinstance(event, RequestStarted):
                self._requests_done += 1
                self._requesting = True
                self._wait_reason = None
                self._wait_until = None
            elif isinstance(event, RetryScheduled):
                self._retry += 1
            elif isinstance(event, OutagePaused):
                self._outage_state = "paused"
                self._done_at_frozen = self._compute_done_at()
            elif isinstance(event, OutageProbing):
                self._outage_state = "probing"
                self._outage_down = event.down_seconds
                self._outage_probe_in = event.probe_in_seconds
            elif isinstance(event, OutageRecovered):
                self._outage_state = ""
                self._outage_down = 0.0
                self._outage_probe_in = 0.0
                self._done_at_frozen = None
            elif isinstance(event, OutageGaveUp):
                self._outage_state = "gave_up"
                self._outage_down = event.down_seconds
            elif isinstance(event, Cancelled):
                self._cancelled = True
            elif isinstance(event, RunFinished):
                self._finished = True
                self._incomplete = event.incomplete
                self._wait_reason = None
                self._wait_until = None
                self._requesting = False

    def snapshot(self) -> Snapshot:
        """Copy the current state into an immutable :class:`Snapshot`."""
        with self._lock:
            elapsed = self._elapsed_locked()
            rate = (
                (self._requests_done / elapsed * 60.0)
                if elapsed > 0 and self._requests_done > 0
                else None
            )
            eta = self._eta_locked()
            done_at = self._done_at_locked(eta)
            return Snapshot(
                run_name=self._run_name,
                courts=self._courts,
                phase_name=self._phase_name,
                phase_index=self._phase_index,
                phase_total=self._phase_total,
                phase_done=self._phase_done,
                phase_items=self._phase_items,
                requests_done=self._requests_done,
                est_total_requests=self._est_total_requests,
                ok=self._ok,
                skipped=self._skipped,
                error=self._error,
                deferred=self._deferred,
                retry=self._retry,
                current_label=self._current_label,
                wait_reason=self._wait_reason,
                wait_until_monotonic=self._wait_until,
                requesting=self._requesting,
                outage_state=self._outage_state,
                outage_down_seconds=self._outage_down,
                outage_probe_in=self._outage_probe_in,
                cancelled=self._cancelled,
                finished=self._finished,
                incomplete=self._incomplete,
                eta_seconds=eta,
                done_at_wall=done_at,
                elapsed_seconds=elapsed,
                rate_per_min=rate,
            )

    # -- ETA helpers (all called under the lock) ---------------------------
    def _elapsed_locked(self) -> float:
        if self._start_monotonic is None:
            return 0.0
        return max(0.0, self._monotonic() - self._start_monotonic)

    def _pace_locked(self) -> float:
        """Seconds per request: config floor, blended with observed pace late."""
        floor = self._delay + self._jitter / 2.0
        if self._requests_done >= _ETA_BLEND_AFTER:
            elapsed = self._elapsed_locked()
            observed = elapsed / self._requests_done if self._requests_done else floor
            return 0.5 * floor + 0.5 * observed
        return floor

    def _eta_locked(self) -> float | None:
        if self._est_total_requests <= 0 or self._finished:
            return None
        remaining = max(0, self._est_total_requests - self._requests_done)
        return remaining * self._pace_locked()

    def _compute_done_at(self) -> float | None:
        eta = self._eta_locked()
        if eta is None or self._start_wall is None:
            return None
        return self._wall() + eta

    def _done_at_locked(self, eta: float | None) -> float | None:
        # During an outage the wall-clock finish time is frozen so it does not
        # flicker every frame while the crawl is paused.
        if self._outage_state in ("paused", "probing") and self._done_at_frozen:
            return self._done_at_frozen
        if eta is None:
            return None
        return self._wall() + eta
