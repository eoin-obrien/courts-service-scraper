"""Tests for the render sinks: dashboard, plain lines, and reporter selection."""

from __future__ import annotations

import io
import threading
from types import SimpleNamespace

from rich.console import Console

from courts_scraper.progress import (
    Cancelled,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    OutageGaveUp,
    OutagePaused,
    OutageProbing,
    OutageRecovered,
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
    _countdown_text,
    _eta_text,
    _TrailerColumn,
    glyphs_for,
    scroll_line_for,
)
from courts_scraper.progress.plain import PlainReporter
from courts_scraper.progress.reporter import ProgressReporter
from courts_scraper.progress.select import LiveDashboardReporter as _Live
from courts_scraper.progress.select import select_reporter


class _MutClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# -- glyphs ----------------------------------------------------------------
def test_glyphs_for_picks_unicode_then_ascii():
    assert glyphs_for("utf-8") is UNICODE
    assert glyphs_for("UTF-8") is UNICODE
    assert glyphs_for("ascii") is ASCII
    assert glyphs_for(None) is ASCII


# -- scroll_line_for (the curated "what scrolls" policy) -------------------
def test_scroll_line_notable_events_produce_lines():
    g = UNICODE
    assert scroll_line_for(
        RunStarted("run-x", ("Supreme Court",), ("listing",), 10), g
    ).endswith("run-x")
    assert (
        scroll_line_for(PhaseStarted("downloads", 2712), g) == "-- downloads 2,712 --"
    )
    assert "skipped" in scroll_line_for(
        ItemFinished("m", ItemStatus.SKIPPED_NO_CITATION, "In re Estate"), g
    )
    assert "error" in scroll_line_for(ItemFinished("m", ItemStatus.ERROR, "boom"), g)
    assert (
        "retry"
        in scroll_line_for(ItemFinished("d", ItemStatus.DEFERRED, "flaky"), g).lower()
    )
    assert scroll_line_for(OutagePaused(3), g) is not None
    assert scroll_line_for(OutageProbing(60.0, 120.0), g) is not None
    assert scroll_line_for(OutageRecovered(240.0), g) is not None
    assert scroll_line_for(OutageGaveUp(3600.0), g) is not None
    assert scroll_line_for(Cancelled("downloads"), g) is not None
    assert "complete" in scroll_line_for(
        RunFinished({"download_done": 5}, 10.0, incomplete=False), g
    )


def test_scroll_line_noisy_events_are_silent():
    g = UNICODE
    # These carry the bars, not the scroll log -- no per-item / per-request spam.
    assert scroll_line_for(ItemFinished("m", ItemStatus.OK), g) is None
    assert scroll_line_for(ItemStarted("m", "[2026] IESC 1", "u"), g) is None
    assert scroll_line_for(RequestStarted("u"), g) is None
    assert scroll_line_for(WaitStarted(WaitReason.POLITENESS, 6.0, 100.0), g) is None


# -- countdown / eta columns ----------------------------------------------
def _snap_waiting(until: float):
    model = ProgressModel(delay=5.0, jitter=1.0)
    model.apply(WaitStarted(WaitReason.POLITENESS, 6.0, until))
    return model.snapshot()


def test_countdown_animates_and_never_shows_zero():
    snap = _snap_waiting(100.0)
    assert "3.0s" in _countdown_text(snap, 97.0, 0.15, UNICODE)
    assert "1.0s" in _countdown_text(snap, 99.0, 0.15, UNICODE)
    near_zero = _countdown_text(snap, 99.99, 0.15, UNICODE)
    assert "0.0s" not in near_zero
    assert "requesting" in near_zero


def test_countdown_shows_request_in_flight_and_outage():
    model = ProgressModel(delay=5.0, jitter=1.0)
    model.apply(RequestStarted("u"))
    assert "flight" in _countdown_text(model.snapshot(), 0.0, 0.15, UNICODE)
    model.apply(OutagePaused(3))
    model.apply(OutageProbing(60.0, 118.0))
    assert "probing" in _countdown_text(model.snapshot(), 0.0, 0.15, UNICODE)


def test_eta_text_states():
    model = ProgressModel(
        delay=5.0, jitter=2.0, monotonic=lambda: 0.0, wall=lambda: 0.0
    )
    model.apply(RunStarted("r", (), ("downloads",), 100))
    assert _eta_text(model.snapshot()).startswith("ETA ")
    model.apply(RunFinished({}, 1.0, incomplete=False))
    assert _eta_text(model.snapshot()) == "complete"


# -- LiveDashboardReporter integration (record=True + export_text) ---------
def _record_console() -> Console:
    return Console(file=io.StringIO(), record=True, force_terminal=True, width=100)


def test_dashboard_renders_bars_and_curated_lines():
    console = _record_console()
    with LiveDashboardReporter(console, delay=5.0, jitter=1.0) as reporter:
        reporter.emit(
            RunStarted("20260722T__supreme", ("Supreme Court",), ("downloads",), 10)
        )
        reporter.emit(PhaseStarted("downloads", 5))
        for i in range(5):
            reporter.emit(ItemStarted("downloads", f"[2026] IESC {i}", "u"))
            reporter.emit(RequestStarted("u"))
            reporter.emit(ItemFinished("downloads", ItemStatus.OK))
        reporter.emit(
            ItemFinished("downloads", ItemStatus.SKIPPED_NO_CITATION, "No cite")
        )
        reporter.emit(
            RunFinished(
                {"download_done": 5, "download_error": 0}, 3.0, incomplete=False
            )
        )
    text = console.export_text()
    assert "overall" in text  # the overall bar
    assert "downloads" in text  # the phase bar / header
    assert "-- downloads 5 --" in text  # phase header scrolled
    assert "skipped: No cite" in text  # curated skip line
    assert "run complete" in text  # final summary


def test_dashboard_rows_fit_terminal_width():
    # AC-9: even a long citation must not wrap the phase or overall row at 80 cols.
    # Measure ONE clean frame of the two bars (not the live session, whose
    # export_text glues successive frames together -- the spike's capture caveat).
    work = Console(file=io.StringIO(), force_terminal=True, width=80)
    reporter = LiveDashboardReporter(work, delay=5.0, jitter=1.0, monotonic=lambda: 0.0)
    reporter.emit(RunStarted("run", ("Supreme Court",), ("downloads",), 5694))
    reporter.emit(PhaseStarted("downloads", 2712))
    reporter.emit(
        ItemStarted(
            "downloads",
            "[2024] IESC 12 - DPP v. Murphy & Ors (a very long title indeed)",
            "u",
        )
    )
    reporter.emit(WaitStarted(WaitReason.POLITENESS, 3.8, 3.8))
    reporter._sync_bars()
    frame = Console(file=io.StringIO(), record=True, force_terminal=True, width=80)
    frame.print(reporter._progress.get_renderable())
    rows = [line for line in frame.export_text().splitlines() if line.strip()]
    assert len(rows) == 2  # exactly the two bars, each on its own line (no wrap)
    for line in rows:
        assert len(line.rstrip()) <= 80, repr(line)


def test_dashboard_handles_unknown_phase_total():
    # Listing with an unknown result count -> phase total 0 -> indeterminate bar.
    console = _record_console()
    with LiveDashboardReporter(console, delay=5.0, jitter=1.0) as reporter:
        reporter.emit(RunStarted("r", (), ("listing",), 0))
        reporter.emit(PhaseStarted("listing", 0))  # unknown total
        reporter.emit(ItemStarted("listing", "page 1", ""))
        reporter.emit(ItemFinished("listing", ItemStatus.OK))
    assert "listing" in console.export_text()  # rendered, no crash


def test_dashboard_milestone_heartbeat():
    console = _record_console()
    with LiveDashboardReporter(console, delay=0.0, jitter=0.0) as reporter:
        reporter.emit(RunStarted("r", (), ("downloads",), 1000))
        reporter.emit(PhaseStarted("downloads", 1000))
        for _ in range(500):
            reporter.emit(ItemFinished("downloads", ItemStatus.OK))
    assert "500 done" in console.export_text()


def test_trailer_column_render_is_race_free_under_mutation():
    """AC-11/H4: the refresh thread's column read must never raise mid-mutation."""
    model = ProgressModel(delay=5.0, jitter=1.0)
    model.apply(RunStarted("r", (), ("downloads",), 1000))
    column = _TrailerColumn(model, clamp=0.15, glyphs=UNICODE, monotonic=lambda: 0.0)
    task = SimpleNamespace(fields={"kind": "phase"})
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
        column.render(task)  # type: ignore[arg-type]
    t.join()
    assert errors == []


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
