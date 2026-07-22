"""The live terminal dashboard: a ``rich.Live`` sink over a :class:`ProgressModel`.

The engine thread feeds events through :meth:`LiveDashboardReporter.emit`; a
separate ``rich.Live`` refresh thread redraws at a fixed rate from an immutable
:meth:`ProgressModel.snapshot`, so the countdown animates even while the engine is
blocked in a politeness ``sleep()`` (proven by the P0 spike). Rendering is a pure
function of a snapshot -- :func:`render_dashboard` -- so every display state can be
unit-tested without a terminal or a real crawl.

Two robustness details the spike and review surfaced:

* the countdown is clamped near zero to the transitional "requesting" state, so the
  unavoidable ``sleep()``->``RequestStarted`` boundary race can never render a
  frozen ``0.0s``; and
* glyphs fall back to ASCII when the output encoding is not UTF-8, and colour is
  dropped when ``NO_COLOR`` is set.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from courts_scraper.progress.events import Event
from courts_scraper.progress.format import format_clock, format_duration
from courts_scraper.progress.model import ProgressModel, Snapshot

_OVERALL_BAR_WIDTH = 28
_PHASE_BAR_WIDTH = 20


@dataclass(frozen=True, slots=True)
class Glyphs:
    """A set of drawing glyphs, with a UTF-8 and an ASCII variant."""

    fill: str
    empty: str
    left: str
    right: str
    dot: str
    down: str
    check: str
    warn: str
    retry: str
    pause: str


UNICODE = Glyphs("█", "░", "▕", "▏", "●", "↓", "✓", "⚠", "↺", "⏸")
ASCII = Glyphs("#", "-", "[", "]", "*", ">", "v", "!", "~", "||")


def glyphs_for(encoding: str | None) -> Glyphs:
    """Pick the UTF-8 glyph set when the encoding supports it, else ASCII."""
    return UNICODE if encoding and "utf" in encoding.lower() else ASCII


def _bar(fraction: float, width: int, g: Glyphs) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = round(fraction * width)
    return f"{g.left}{g.fill * filled}{g.empty * (width - filled)}{g.right}"


def _countdown_text(snap: Snapshot, now: float, clamp: float, g: Glyphs) -> str:
    """The "Now / Next request" line, honouring the near-zero clamp (H2)."""
    if snap.requesting:
        return f"{g.dot} request in flight"
    if snap.wait_until_monotonic is not None:
        remaining = max(0.0, snap.wait_until_monotonic - now)
        if remaining <= clamp:
            # Within a frame of zero the request is imminent; never show 0.0s.
            return f"{g.dot} requesting..."
        reason = (snap.wait_reason.value if snap.wait_reason else "wait").replace(
            "_", " "
        )
        return f"next {reason} request in {remaining:4.1f}s"
    return ""


def render_dashboard(
    snap: Snapshot, *, glyphs: Glyphs, now: float, clamp: float
) -> RenderableType:
    """Build the dashboard renderable for one snapshot (pure, testable)."""
    g = glyphs
    lines: list[RenderableType] = []

    title = f"courts-scraper {'/'.join(snap.courts) or 'run'} - {snap.run_name}".strip()
    lines.append(Text(title, style="bold"))

    # ETA is the primary datum for a multi-hour run: give it the top line.
    if snap.finished:
        state = "incomplete - resume to finish" if snap.incomplete else "complete"
        lines.append(Text(f"Run {state}", style="bold green"))
    elif snap.eta_seconds is not None:
        done_at = (
            f" - done ~{format_clock(snap.done_at_wall)}"
            if snap.done_at_wall is not None
            else ""
        )
        lines.append(
            Text(f"ETA {format_duration(snap.eta_seconds)}{done_at}", style="bold")
        )
    else:
        lines.append(Text("ETA - (estimating)", style="bold"))

    # Overall request progress.
    if snap.est_total_requests > 0:
        frac = snap.requests_done / snap.est_total_requests
        lines.append(
            Text(
                f"Overall {_bar(frac, _OVERALL_BAR_WIDTH, g)} {frac * 100:4.1f}% "
                f"- {snap.requests_done:,}/{snap.est_total_requests:,} req"
            )
        )

    # Phase progress; the listing phase can have an unknown item total.
    if snap.phase_name:
        if snap.phase_items > 0:
            pfrac = snap.phase_done / snap.phase_items
            bar = _bar(pfrac, _PHASE_BAR_WIDTH, g)
            phase_bar = f"{bar} {snap.phase_done}/{snap.phase_items}"
        else:
            phase_bar = f"{snap.phase_done} done"
        pos = f"{snap.phase_index}/{snap.phase_total}" if snap.phase_total else ""
        lines.append(Text(f"Phase {pos} {snap.phase_name}  {phase_bar}"))

    # Outage banner replaces the live "Now/Next" rows while paused.
    if snap.outage_state in ("paused", "probing"):
        lines.append(
            Text(
                f"{g.pause} PAUSED - site appears down; probing in "
                f"{int(snap.outage_probe_in)}s "
                f"(down {format_duration(snap.outage_down_seconds)})",
                style="yellow",
            )
        )
    elif snap.outage_state == "gave_up":
        lines.append(
            Text(
                f"{g.pause} site still down after the cap; stopping (resume later)",
                style="red",
            )
        )
    elif snap.cancelled:
        lines.append(Text("stopping cleanly (cancelled)...", style="yellow"))
    elif not snap.finished:
        if snap.current_label:
            lines.append(Text(f"Now  {g.down} {snap.current_label}"))
        countdown = _countdown_text(snap, now, clamp, g)
        rate = f"  rate {snap.rate_per_min:.1f}/min" if snap.rate_per_min else ""
        if countdown or rate:
            lines.append(Text(f"{countdown}{rate}"))

    # Health tally.
    lines.append(
        Text(
            f"{g.check} {snap.ok:,} done   {g.warn} {snap.skipped:,} skipped   "
            f"{snap.error:,} error   {g.retry} {snap.retry:,} retry",
            style="dim",
        )
    )
    return Group(*lines)


class LiveDashboardReporter:
    """A ``rich.Live`` dashboard fed by progress events."""

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
            refresh_hz: Live refresh rate; also sets the countdown clamp.
            monotonic: Monotonic clock (injectable for tests).
        """
        self._console = console
        self._model = ProgressModel(delay=delay, jitter=jitter, monotonic=monotonic)
        self._monotonic = monotonic
        self._refresh_hz = refresh_hz
        self._clamp = 1.0 / refresh_hz + 0.05
        self._glyphs = glyphs_for(console.encoding)
        # get_renderable (not a static frame) so Live's refresh thread recomputes
        # the snapshot and the clock every tick -- this is what animates the
        # countdown *while the engine thread is blocked in a politeness sleep()*,
        # when no events are being emitted (the P0 spike's mechanism).
        self._live = Live(
            get_renderable=self._renderable,
            console=console,
            refresh_per_second=refresh_hz,
            transient=False,
        )

    def _renderable(self) -> RenderableType:
        return render_dashboard(
            self._model.snapshot(),
            glyphs=self._glyphs,
            now=self._monotonic(),
            clamp=self._clamp,
        )

    def emit(self, event: Event) -> None:
        """Fold ``event`` into the model and force an immediate redraw."""
        self._model.apply(event)
        # Redraw now so a state change shows instantly rather than at the next tick;
        # get_renderable pulls the fresh snapshot.
        self._live.refresh()

    def __enter__(self) -> LiveDashboardReporter:
        """Start the live display."""
        self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Render a final frame and restore the terminal, even on interrupt."""
        # A final refresh so the completed/cancelled frame is the last thing left
        # on screen, then hand off to Live to restore the cursor.
        try:
            self._live.refresh()
        finally:
            self._live.__exit__(exc_type, exc, tb)


def env_no_color() -> bool:
    """Whether ``NO_COLOR`` is set in the environment (any value)."""
    return "NO_COLOR" in os.environ
