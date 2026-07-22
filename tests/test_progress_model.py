"""Unit tests for the progress event vocabulary, model, and null reporter."""

from __future__ import annotations

import threading

from courts_scraper.progress import (
    Cancelled,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    OutagePaused,
    OutageProbing,
    OutageRecovered,
    ProgressModel,
    QuietReporter,
    RequestStarted,
    RetryScheduled,
    RunFinished,
    RunStarted,
    WaitReason,
    WaitStarted,
)


class _Clock:
    """A hand-cranked monotonic clock for deterministic ETA tests."""

    def __init__(self) -> None:
        self.t = 1000.0

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return 5000.0


def _model(clock: _Clock, *, delay: float = 5.0, jitter: float = 2.0) -> ProgressModel:
    return ProgressModel(
        delay=delay, jitter=jitter, monotonic=clock.mono, wall=clock.wall
    )


def test_eta_uses_floor_pace_before_twenty_requests():
    clock = _Clock()
    model = _model(clock)
    model.apply(RunStarted("run", ("Supreme Court",), ("listing",), 10))
    # floor pace = delay + jitter/2 = 6; 10 remaining -> 60s.
    assert model.snapshot().eta_seconds == 60.0
    assert model.snapshot().done_at_wall == 5000.0 + 60.0


def test_eta_blends_observed_pace_after_twenty_requests():
    clock = _Clock()
    model = _model(clock)
    model.apply(RunStarted("run", ("Supreme Court",), ("downloads",), 100))
    for _ in range(20):
        model.apply(RequestStarted("u"))
    clock.t += 60.0  # 60s elapsed over 20 requests -> observed pace 3s/req
    # blended pace = 0.5*6 + 0.5*3 = 4.5; remaining 80 -> 360s.
    assert model.snapshot().eta_seconds == 360.0


def test_eta_is_none_when_estimate_unknown_or_finished():
    clock = _Clock()
    model = _model(clock)
    model.apply(RunStarted("run", (), (), 0))
    assert model.snapshot().eta_seconds is None
    model2 = _model(_Clock())
    model2.apply(RunStarted("run", (), (), 10))
    model2.apply(RunFinished({}, 1.0, incomplete=False))
    assert model2.snapshot().eta_seconds is None


def test_five_outcomes_stay_distinct():
    model = _model(_Clock())
    model.apply(ItemFinished("metadata", ItemStatus.OK))
    model.apply(ItemFinished("metadata", ItemStatus.SKIPPED_NO_CITATION))
    model.apply(ItemFinished("metadata", ItemStatus.ERROR))
    model.apply(ItemFinished("downloads", ItemStatus.DEFERRED))
    model.apply(RetryScheduled("u", 1, 4, 2.0))
    snap = model.snapshot()
    assert (snap.ok, snap.skipped, snap.error, snap.deferred, snap.retry) == (
        1,
        1,
        1,
        1,
        1,
    )


def test_wait_then_request_transition():
    clock = _Clock()
    model = _model(clock)
    model.apply(ItemStarted("downloads", "[2026] IESC 1", "u"))
    model.apply(WaitStarted(WaitReason.POLITENESS, 6.0, clock.t + 6.0))
    snap = model.snapshot()
    assert snap.current_label == "[2026] IESC 1"
    assert snap.wait_reason is WaitReason.POLITENESS
    assert snap.wait_until_monotonic == clock.t + 6.0
    assert snap.requesting is False
    # RequestStarted clears the countdown and marks the request in flight.
    model.apply(RequestStarted("u"))
    snap = model.snapshot()
    assert snap.wait_until_monotonic is None
    assert snap.requesting is True
    assert snap.requests_done == 1


def test_done_at_frozen_during_outage():
    clock = _Clock()
    model = _model(clock)
    model.apply(RunStarted("run", (), ("downloads",), 100))
    frozen = model.snapshot().done_at_wall
    model.apply(OutagePaused(3))
    model.apply(OutageProbing(120.0, 120.0))
    # More requests would normally move done_at, but it is frozen while paused.
    model.apply(RequestStarted("u"))
    assert model.snapshot().done_at_wall == frozen
    assert model.snapshot().outage_state == "probing"
    model.apply(OutageRecovered(240.0))
    assert model.snapshot().outage_state == ""
    # Unfrozen: recomputed (fewer remaining -> sooner or equal).
    assert model.snapshot().done_at_wall is not None


def test_phase_done_resets_per_phase():
    model = _model(_Clock())
    model.apply(RunStarted("run", (), ("metadata", "downloads"), 4))
    model.apply(ItemFinished("metadata", ItemStatus.OK))
    model.apply(ItemFinished("metadata", ItemStatus.OK))
    from courts_scraper.progress import PhaseStarted

    model.apply(PhaseStarted("downloads", 2, 2, 3))
    assert model.snapshot().phase_done == 0
    model.apply(ItemFinished("downloads", ItemStatus.OK))
    assert model.snapshot().phase_done == 1


def test_cancelled_flag():
    model = _model(_Clock())
    model.apply(Cancelled("downloads"))
    assert model.snapshot().cancelled is True


def test_snapshot_is_race_free_under_concurrent_mutation():
    """H4: reading snapshots while a worker floods events must never raise."""
    model = _model(_Clock())
    model.apply(RunStarted("run", (), ("downloads",), 1000))
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
    for _ in range(2000):
        model.snapshot()  # must never raise despite concurrent mutation
    t.join()
    assert errors == []
    assert model.snapshot().ok == 1000


def test_quiet_reporter_is_a_noop_context_manager():
    with QuietReporter() as reporter:
        reporter.emit(ItemFinished("metadata", ItemStatus.ERROR))
        reporter.emit(RunFinished({}, 1.0, incomplete=True))
    # No output, no error log written here -- the engine owns the durable log.
