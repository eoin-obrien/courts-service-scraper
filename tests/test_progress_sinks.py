"""Tests for the render sinks: dashboard, plain lines, and reporter selection."""

from __future__ import annotations

import io
import threading

from rich.console import Console, RenderableType

from courts_scraper.progress import (
    Cancelled,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    OutageGaveUp,
    OutagePaused,
    OutageProbing,
    PhaseStarted,
    ProgressModel,
    QuietReporter,
    RequestStarted,
    RunFinished,
    RunStarted,
    WaitReason,
    WaitStarted,
)
from courts_scraper.progress.dashboard import (
    ASCII,
    UNICODE,
    LiveDashboardReporter,
    glyphs_for,
    render_dashboard,
)
from courts_scraper.progress.plain import PlainReporter
from courts_scraper.progress.reporter import ProgressReporter
from courts_scraper.progress.select import LiveDashboardReporter as _Live
from courts_scraper.progress.select import select_reporter


def _text(renderable: RenderableType) -> str:
    console = Console(file=io.StringIO(), width=100)
    console.print(renderable)
    return console.file.getvalue()  # type: ignore[attr-defined]


def _render(model: ProgressModel, *, now: float = 0.0) -> str:
    return _text(
        render_dashboard(model.snapshot(), glyphs=UNICODE, now=now, clamp=0.15)
    )


# -- glyphs ----------------------------------------------------------------
def test_glyphs_for_picks_unicode_then_ascii():
    assert glyphs_for("utf-8") is UNICODE
    assert glyphs_for("UTF-8") is UNICODE
    assert glyphs_for("ascii") is ASCII
    assert glyphs_for(None) is ASCII


# -- dashboard states ------------------------------------------------------
def test_eta_is_rendered_prominently():
    model = ProgressModel(
        delay=5.0, jitter=2.0, monotonic=lambda: 0.0, wall=lambda: 0.0
    )
    model.apply(RunStarted("run", ("Supreme Court",), ("downloads",), 100))
    out = _render(model)
    assert "ETA" in out
    assert "Overall" in out


def test_countdown_clamps_near_zero_and_never_shows_zero():
    model = ProgressModel(delay=5.0, jitter=1.0)
    model.apply(ItemStarted("downloads", "[2026] IESC 1", "u"))
    model.apply(WaitStarted(WaitReason.POLITENESS, 6.0, 100.0))
    # 3s out: a live countdown.
    assert "3.0s" in _render(model, now=97.0)
    # Within a frame of zero: the transitional state, never "0.0s".
    near_zero = _render(model, now=99.99)
    assert "0.0s" not in near_zero
    assert "requesting" in near_zero
    # Request in flight (RequestStarted cleared the wait).
    model.apply(RequestStarted("u"))
    flight = _render(model, now=100.0)
    assert "0.0s" not in flight
    assert "flight" in flight


def test_outage_banner_replaces_now_line():
    model = ProgressModel(delay=5.0, jitter=1.0)
    model.apply(RunStarted("run", (), ("downloads",), 100))
    model.apply(ItemStarted("downloads", "some.pdf", "u"))
    model.apply(OutagePaused(3))
    model.apply(OutageProbing(60.0, 120.0))
    out = _render(model)
    assert "PAUSED" in out
    assert "some.pdf" not in out  # the Now line is suppressed while paused


def test_all_states_render_without_error():
    # A battery of crafted states; each must render to non-empty text.
    states: list[ProgressModel] = []

    def fresh() -> ProgressModel:
        return ProgressModel(delay=5.0, jitter=1.0)

    startup = fresh()
    startup.apply(RunStarted("r", ("Supreme Court",), ("listing", "metadata"), 50))
    states.append(startup)

    listing_unknown = fresh()
    listing_unknown.apply(RunStarted("r", (), ("listing",), 0))
    listing_unknown.apply(PhaseStarted("listing", 0))  # unknown total
    listing_unknown.apply(ItemStarted("listing", "page 1", ""))
    states.append(listing_unknown)

    single = fresh()
    single.apply(RunStarted("r", (), ("downloads",), 1))
    single.apply(PhaseStarted("downloads", 1))
    states.append(single)

    zero = fresh()
    zero.apply(RunStarted("r", (), ("downloads",), 0))
    zero.apply(PhaseStarted("downloads", 0))
    states.append(zero)

    cancelled = fresh()
    cancelled.apply(RunStarted("r", (), ("downloads",), 5))
    cancelled.apply(Cancelled("downloads"))
    states.append(cancelled)

    gave_up = fresh()
    gave_up.apply(RunStarted("r", (), ("downloads",), 5))
    gave_up.apply(OutageGaveUp(3600.0))
    states.append(gave_up)

    completed = fresh()
    completed.apply(RunStarted("r", (), ("downloads",), 5))
    completed.apply(RunFinished({"download_done": 5}, 10.0, incomplete=False))
    states.append(completed)

    for model in states:
        assert _render(model).strip() != ""


def test_render_is_race_free_under_concurrent_mutation():
    """AC-5: rendering while the engine mutates the model must never raise."""
    model = ProgressModel(delay=5.0, jitter=1.0)
    model.apply(RunStarted("r", (), ("downloads",), 1000))
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(1000):
                model.apply(RequestStarted("u"))
                model.apply(ItemFinished("downloads", ItemStatus.OK))
        except BaseException as exc:  # capture for the assertion
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(1000):
        _render(model)
    t.join()
    assert errors == []


def test_live_dashboard_reporter_enter_emit_exit():
    console = Console(file=io.StringIO(), force_terminal=True, width=100)
    with LiveDashboardReporter(console, delay=5.0, jitter=1.0) as reporter:
        reporter.emit(RunStarted("run", ("Supreme Court",), ("downloads",), 10))
        reporter.emit(ItemStarted("downloads", "a.pdf", "u"))
        reporter.emit(RunFinished({"download_done": 1}, 1.0, incomplete=False))
    # A frame was drawn and the terminal control sequences were emitted/closed.
    assert console.file.getvalue() != ""  # type: ignore[attr-defined]


class _MutClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_live_renderable_recomputes_each_call_so_countdown_animates():
    # The refresh thread calls get_renderable (== _renderable) every tick with NO
    # new event; it must reflect the current clock, or the countdown freezes.
    clock = _MutClock()
    console = Console(file=io.StringIO(), force_terminal=True, width=80)
    reporter = LiveDashboardReporter(console, delay=5.0, jitter=0.0, monotonic=clock)
    reporter._model.apply(ItemStarted("downloads", "x", "u"))
    reporter._model.apply(WaitStarted(WaitReason.POLITENESS, 5.0, 1005.0))
    clock.t = 1002.0
    first = _text(reporter._renderable())
    clock.t = 1004.0
    second = _text(reporter._renderable())
    assert "3.0s" in first
    assert "1.0s" in second
    assert first != second


# -- plain sink ------------------------------------------------------------
def test_plain_writes_phase_and_final_lines():
    buf = io.StringIO()
    reporter = PlainReporter(buf, delay=0.0, jitter=0.0, every=1)
    reporter.emit(RunStarted("run", (), ("downloads",), 3))
    reporter.emit(PhaseStarted("downloads", 3))
    reporter.emit(ItemFinished("downloads", ItemStatus.OK))
    reporter.emit(
        RunFinished({"download_done": 1, "download_error": 0}, 5.0, incomplete=False)
    )
    out = buf.getvalue()
    assert "downloads" in out
    assert "run complete" in out
    assert "\x1b[" not in out  # no cursor-control escapes -- safe in a log


def test_plain_final_line_falls_back_to_model_elapsed():
    clock = _MutClock()
    buf = io.StringIO()
    reporter = PlainReporter(buf, delay=0.0, jitter=0.0, monotonic=clock)
    reporter.emit(RunStarted("r", (), ("downloads",), 1))  # model starts at t=1000
    clock.t = 1090.0  # 90s later
    reporter.emit(
        RunFinished({"download_done": 1, "download_error": 0}, 0.0, incomplete=False)
    )
    assert "1m 30s" in buf.getvalue()  # not "in 0s"


def test_plain_truncates_long_lines():
    buf = io.StringIO()
    reporter = PlainReporter(buf, delay=0.0, jitter=0.0, width=30)
    reporter.emit(Cancelled("a-phase-with-a-very-long-name-that-exceeds-the-width"))
    line = buf.getvalue().splitlines()[0]
    assert len(line) <= 30
    assert line.endswith("…")


def test_plain_throttles_items():
    buf = io.StringIO()
    reporter = PlainReporter(buf, delay=0.0, jitter=0.0, every=5, min_interval=1e9)
    reporter.emit(PhaseStarted("downloads", 100))
    start_lines = len(buf.getvalue().splitlines())
    for _ in range(4):
        reporter.emit(ItemFinished("downloads", ItemStatus.OK))
    assert len(buf.getvalue().splitlines()) == start_lines  # not yet due
    reporter.emit(ItemFinished("downloads", ItemStatus.OK))  # 5th -> due
    assert len(buf.getvalue().splitlines()) == start_lines + 1


# -- reporter selection ----------------------------------------------------
def _console(*, terminal: bool, width: int) -> Console:
    return Console(file=io.StringIO(), force_terminal=terminal, width=width)


def _is_plain(reporter: ProgressReporter) -> bool:
    return isinstance(reporter, PlainReporter)


def test_select_quiet_wins():
    reporter = select_reporter(
        _console(terminal=True, width=120), quiet=True, delay=5.0, jitter=1.0
    )
    assert isinstance(reporter, QuietReporter)


def test_select_dashboard_on_wide_terminal():
    reporter = select_reporter(
        _console(terminal=True, width=120), quiet=False, delay=5.0, jitter=1.0
    )
    assert isinstance(reporter, _Live)


def test_select_plain_on_narrow_terminal():
    reporter = select_reporter(
        _console(terminal=True, width=40), quiet=False, delay=5.0, jitter=1.0
    )
    assert _is_plain(reporter)


def test_select_plain_on_non_terminal():
    reporter = select_reporter(
        _console(terminal=False, width=120), quiet=False, delay=5.0, jitter=1.0
    )
    assert _is_plain(reporter)


def test_select_plain_when_prefer_plain_even_on_wide_terminal():
    reporter = select_reporter(
        _console(terminal=True, width=120),
        quiet=False,
        delay=5.0,
        jitter=1.0,
        prefer_plain=True,
    )
    assert _is_plain(reporter)
