"""The live terminal dashboard: bottom-pinned ``rich.Progress`` bars + a scroll log.

The engine thread feeds events through :meth:`LiveDashboardReporter.emit`. Two
``rich.Progress`` tasks (an overall bar and the current-phase bar) stay pinned at
the bottom of the terminal, and *notable* events (phase headers, skips, errors,
outages, milestones) scroll **above** them via ``progress.console.print`` -- the
docker/cargo shape. A clean run scrolls calmly; the current judgment rides in the
phase bar's description rather than spamming a line per item.

The "breathing" countdown to the next polite request survives as a custom
``rich.Progress`` column: Progress re-invokes every column on each refresh tick, so
a column that reads the shared :class:`ProgressModel` snapshot animates *while the
engine thread is blocked in a politeness* ``sleep()`` (proven by the P0 spike). The
countdown is clamped near zero to the transitional "requesting" state, so the
unavoidable ``sleep()``->``RequestStarted`` boundary race never renders a frozen
``0.0s``. Colour is dropped when ``NO_COLOR`` is set (via the Console), and the
spinner falls back to an ASCII animation when the output encoding is not UTF-8.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
)
from rich.table import Column
from rich.text import Text

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
    RunFinished,
    RunStarted,
)
from courts_scraper.progress.format import format_clock, format_duration
from courts_scraper.progress.model import ProgressModel, Snapshot

#: Print a milestone line every this many successfully-finished items.
_MILESTONE = 500
#: Fixed width for the description column, so a long citation on the phase row
#: cannot widen the shared column and wrap the (short) overall row past 80 cols.
#: rich ellipsizes any overflow inside this width.
_DESC_MAX = 32
#: Short labels for the countdown, keeping the phase row narrow enough to fit 80c.
_WAIT_LABEL = {"politeness": "next", "retry_backoff": "retry", "outage_probe": "probe"}


@dataclass(frozen=True, slots=True)
class Glyphs:
    """A small set of status glyphs, with a UTF-8 and an ASCII variant."""

    dot: str
    down: str
    check: str
    warn: str
    retry: str
    pause: str
    cross: str


UNICODE = Glyphs("·", "↓", "✓", "⚠", "↺", "⏸", "✗")
ASCII = Glyphs(".", ">", "v", "!", "~", "||", "x")


def glyphs_for(encoding: str | None) -> Glyphs:
    """Pick the UTF-8 glyph set when the encoding supports it, else ASCII."""
    return UNICODE if encoding and "utf" in encoding.lower() else ASCII


def _countdown_text(snap: Snapshot, now: float, clamp: float, g: Glyphs) -> str:
    """The phase bar's trailing countdown, honouring the near-zero clamp (H2).

    Kept pure so its timing behaviour is unit-testable. During an outage it shows
    the probe state instead of a request countdown.
    """
    if snap.outage_state == "probing":
        return f"{g.pause} probing {int(snap.outage_probe_in)}s"
    if snap.outage_state == "paused":
        # Paused but the probe countdown hasn't been armed yet (OutageProbing
        # sets outage_probe_in); showing "probing 0s" here would be a lie.
        return f"{g.pause} site down"
    if snap.requesting:
        return f"{g.dot} request in flight"
    if snap.wait_until_monotonic is not None:
        remaining = max(0.0, snap.wait_until_monotonic - now)
        if remaining <= clamp:
            # Within a frame of zero the request is imminent; never show 0.0s.
            return f"{g.dot} requesting..."
        reason = snap.wait_reason.value if snap.wait_reason else "politeness"
        return f"{_WAIT_LABEL.get(reason, 'next')} in {remaining:4.1f}s"
    return ""


def _eta_text(snap: Snapshot) -> str:
    """The overall bar's trailing ETA + finish-time, from a snapshot."""
    if snap.finished:
        return "complete" if not snap.incomplete else "incomplete"
    if snap.eta_seconds is None:
        return "ETA -"
    done = (
        f" ~{format_clock(snap.done_at_wall)}" if snap.done_at_wall is not None else ""
    )
    return f"ETA {format_duration(snap.eta_seconds)}{done}"


def scroll_line_for(event: Event, g: Glyphs) -> str | None:
    """The curated scroll line for ``event``, or ``None`` to print nothing.

    Pure function of the event (so the "what scrolls" policy is unit-testable
    without a terminal). A clean run only surfaces phase headers, the run title,
    and the final summary; the noisy per-request events (``ok`` finishes, request
    starts, waits) return ``None`` and are reflected only in the bars. The
    milestone heartbeat is emitted by the reporter (it depends on a running count,
    not one event).
    """
    if isinstance(event, RunStarted):
        courts = "/".join(event.courts) or "run"
        return f"{courts}  {event.run_name}"
    if isinstance(event, PhaseStarted):
        size = f"{event.items:,}" if event.items else "?"
        return f"-- {event.phase} {size} --"
    if isinstance(event, ItemFinished):
        if event.status is ItemStatus.SKIPPED_NO_CITATION:
            return f"{g.warn} skipped: {event.detail}"
        if event.status is ItemStatus.ERROR:
            return f"{g.cross} error: {event.detail}"
        if event.status is ItemStatus.DEFERRED:
            return f"{g.retry} deferred: {event.detail} (will retry)"
        return None  # ok -> the bars carry it
    if isinstance(event, OutagePaused):
        return f"{g.pause} site appears down; pausing until it recovers"
    if isinstance(event, OutageProbing):
        return (
            f"  probing again in {int(event.probe_in_seconds)}s "
            f"(down {format_duration(event.down_seconds)})"
        )
    if isinstance(event, OutageRecovered):
        return (
            f"{g.check} site back after {format_duration(event.down_seconds)}; resuming"
        )
    if isinstance(event, OutageGaveUp):
        return f"{g.cross} site still down after the cap; stopping (resume later)"
    if isinstance(event, Cancelled):
        return f"stopping cleanly (cancelled during {event.phase})..."
    if isinstance(event, RunFinished):
        state = "incomplete" if event.incomplete else "complete"
        return (
            f"run {state} in {format_duration(event.elapsed_seconds)} "
            f"({event.counts.get('download_done', 0)} downloaded, "
            f"{event.counts.get('download_error', 0)} to retry)"
        )
    return None


class _TrailerColumn(ProgressColumn):
    """One column shared by both bars; branches on the task's ``kind`` field.

    Reads the shared :class:`ProgressModel` snapshot directly (Progress only hands
    a column the task), so it animates on every refresh tick: ETA for the overall
    task, the live countdown for the phase task.
    """

    def __init__(
        self,
        model: ProgressModel,
        *,
        clamp: float,
        glyphs: Glyphs,
        monotonic: Callable[[], float],
    ) -> None:
        """Wire the column to the model and the clock."""
        # no_wrap so a long ETA/countdown clips instead of wrapping the pinned
        # bar onto a second physical line at narrow widths.
        super().__init__(table_column=Column(no_wrap=True))
        self._model = model
        self._clamp = clamp
        self._glyphs = glyphs
        self._monotonic = monotonic

    def render(self, task: Task) -> Text:
        """Render the trailing text for ``task`` from the current snapshot."""
        snap = self._model.snapshot()
        if task.fields.get("kind") == "overall":
            return Text(_eta_text(snap), style="bold")
        return Text(
            _countdown_text(snap, self._monotonic(), self._clamp, self._glyphs),
            style="cyan",
        )


class LiveDashboardReporter:
    """A bottom-pinned ``rich.Progress`` dashboard fed by progress events."""

    def __init__(
        self,
        console: Console,
        *,
        delay: float,
        jitter: float,
        refresh_hz: int = 10,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the dashboard sink.

        Args:
            console: The (terminal) console to draw on.
            delay: Politeness delay, for the model's ETA.
            jitter: Politeness jitter.
            refresh_hz: Progress refresh rate; also sets the countdown clamp.
            monotonic: Monotonic clock (injectable for tests).
        """
        self._console = console
        self._model = ProgressModel(delay=delay, jitter=jitter, monotonic=monotonic)
        self._glyphs = glyphs_for(console.encoding)
        self._milestone = 0
        # An ASCII spinner when the terminal can't render the default braille one.
        spinner = "dots" if self._glyphs is UNICODE else "line"
        self._progress = Progress(
            SpinnerColumn(spinner_name=spinner),
            # Fixed-width, non-wrapping description so a long citation on the phase
            # row cannot widen the shared column and wrap the (short) overall row.
            # rich ellipsizes any overflow.
            TextColumn(
                "[bold]{task.description}",
                table_column=Column(width=_DESC_MAX, no_wrap=True, overflow="ellipsis"),
            ),
            # bar_width=None makes the bar the *flexible* column: it shrinks to
            # absorb whatever the fixed columns leave, so a 5-digit MofN or a
            # long ETA never pushes the row past the terminal width (the trailer
            # is no_wrap, so it clips rather than wrapping to a second line).
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            _TrailerColumn(
                self._model,
                clamp=1.0 / refresh_hz + 0.05,
                glyphs=self._glyphs,
                monotonic=monotonic,
            ),
            console=console,
            refresh_per_second=refresh_hz,
            transient=False,
        )
        self._overall: TaskID | None = None
        self._phase: TaskID | None = None

    def emit(self, event: Event) -> None:
        """Fold ``event`` into the model, update the bars, and scroll a line."""
        self._model.apply(event)

        if isinstance(event, RunStarted):
            snap = self._model.snapshot()
            self._overall = self._progress.add_task(
                "overall", total=snap.est_total_requests or None, kind="overall"
            )
        elif isinstance(event, PhaseStarted):
            total = event.items or None
            if self._phase is None:
                self._phase = self._progress.add_task(
                    event.phase, total=total, kind="phase"
                )
            else:
                self._progress.reset(self._phase, total=total, description=event.phase)
        elif isinstance(event, ItemStarted) and self._phase is not None:
            self._progress.update(
                self._phase, description=f"{self._glyphs.down} {event.label}"
            )

        self._sync_bars()

        line = scroll_line_for(event, self._glyphs)
        if line is not None:
            self._progress.console.print(line)
        self._maybe_milestone(event)

    def _sync_bars(self) -> None:
        snap = self._model.snapshot()
        if self._overall is not None:
            self._progress.update(
                self._overall,
                completed=snap.requests_done,
                total=snap.est_total_requests or None,
            )
        if self._phase is not None:
            self._progress.update(
                self._phase,
                completed=snap.phase_done,
                total=snap.phase_items or None,
            )

    def _maybe_milestone(self, event: Event) -> None:
        if not (isinstance(event, ItemFinished) and event.status is ItemStatus.OK):
            return
        ok = self._model.snapshot().ok
        if ok and ok - self._milestone >= _MILESTONE:
            self._milestone = ok - (ok % _MILESTONE)
            self._progress.console.print(f"{self._glyphs.dot} {ok:,} done")

    def __enter__(self) -> LiveDashboardReporter:
        """Start the pinned progress display."""
        self._progress.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the progress display, restoring the terminal (even on interrupt)."""
        self._progress.__exit__(exc_type, exc, tb)


def env_no_color() -> bool:
    """Whether ``NO_COLOR`` is set in the environment (any value)."""
    return "NO_COLOR" in os.environ
