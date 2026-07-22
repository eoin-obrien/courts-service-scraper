"""A plain, newline-appended progress sink for logs, pipes and narrow terminals.

Chosen automatically when stdout is not a wide terminal (piped, cron, CI, or a
terminal under 80 columns). It never emits cursor-control codes -- every update is
a fresh line -- so the output is safe to redirect into a log file, and it stays
readable when an operator tails it. Lines are throttled (every Nth item, and at
least once a minute) so a multi-hour run does not flood the log, and are emitted
eagerly on the events that matter: phase changes, outages, cancellation and the
final summary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType
from typing import IO

from courts_scraper.progress.events import (
    Cancelled,
    Event,
    ItemFinished,
    OutageGaveUp,
    OutagePaused,
    OutageProbing,
    OutageRecovered,
    PhaseStarted,
    RunFinished,
)
from courts_scraper.progress.format import format_duration
from courts_scraper.progress.model import ProgressModel

#: Fallback width when the stream reports none (e.g. a pipe).
_DEFAULT_WIDTH = 200


class PlainReporter:
    """Render progress as throttled, newline-appended status lines."""

    def __init__(
        self,
        stream: IO[str],
        *,
        delay: float,
        jitter: float,
        every: int = 25,
        min_interval: float = 60.0,
        width: int = _DEFAULT_WIDTH,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the sink.

        Args:
            stream: Where lines are written (usually ``stderr``).
            delay: Politeness delay, for the ETA the model computes.
            jitter: Politeness jitter.
            every: Emit a line at most once per this many finished items.
            min_interval: Also emit at least once per this many seconds.
            width: Truncate lines to this many columns.
            monotonic: Monotonic clock (injectable for tests).
        """
        self._stream = stream
        self._model = ProgressModel(delay=delay, jitter=jitter, monotonic=monotonic)
        self._every = max(1, every)
        self._min_interval = min_interval
        self._width = max(20, width)
        self._monotonic = monotonic
        self._items_since = 0
        self._last_emit = monotonic()

    def emit(self, event: Event) -> None:
        """Fold ``event`` into the model and write a line when warranted."""
        self._model.apply(event)
        if isinstance(event, PhaseStarted):
            self._write(self._progress_line())
        elif isinstance(event, ItemFinished):
            self._items_since += 1
            due = self._items_since >= self._every
            stale = (self._monotonic() - self._last_emit) >= self._min_interval
            if due or stale:
                self._write(self._progress_line())
        elif isinstance(event, OutagePaused):
            self._write("PAUSED: site appears down; pausing until it recovers")
        elif isinstance(event, OutageProbing):
            self._write(
                f"  probing again in {int(event.probe_in_seconds)}s "
                f"(down ~{format_duration(event.down_seconds)})"
            )
        elif isinstance(event, OutageRecovered):
            self._write(f"site back after ~{format_duration(event.down_seconds)}")
        elif isinstance(event, OutageGaveUp):
            self._write("site still down after the outage cap; stopping (resume later)")
        elif isinstance(event, Cancelled):
            self._write(f"cancelled during {event.phase}")
        elif isinstance(event, RunFinished):
            self._write(self._final_line(event))

    def _progress_line(self) -> str:
        snap = self._model.snapshot()
        eta = (
            f"eta {format_duration(snap.eta_seconds)}"
            if snap.eta_seconds is not None
            else "eta ?"
        )
        phase = f"{snap.phase_name} {snap.phase_index}/{snap.phase_total}".strip()
        return f"[{phase}] {snap.phase_done}/{snap.phase_items} | {eta}"

    def _final_line(self, event: RunFinished) -> str:
        state = "incomplete" if event.incomplete else "complete"
        return (
            f"run {state} in {format_duration(event.elapsed_seconds)} "
            f"({event.counts.get('download_done', 0)} downloaded, "
            f"{event.counts.get('download_error', 0)} to retry)"
        )

    def _write(self, line: str) -> None:
        self._items_since = 0
        self._last_emit = self._monotonic()
        if len(line) > self._width:
            line = line[: self._width - 1] + "…"
        self._stream.write(line + "\n")
        self._stream.flush()

    def __enter__(self) -> PlainReporter:
        """Return self; nothing to start."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Flush the stream on the way out."""
        self._stream.flush()
