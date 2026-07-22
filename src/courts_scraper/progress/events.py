"""Typed progress events emitted by the crawl engine.

Every event is a frozen dataclass so a reporter can pattern-match on type and a
consumer never has to parse a free-text string to recover meaning. In particular
the five outcomes the engine distinguishes today stay distinct: a missing-citation
skip, a parse/database error, and a retry-later deferral are three different
:class:`ItemStatus` values on :class:`ItemFinished`; an outage give-up is
:class:`OutageGaveUp`; a user cancellation is :class:`Cancelled`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ItemStatus(StrEnum):
    """How a single work item (metadata row or PDF download) finished.

    Kept as distinct values rather than collapsed into one "error" so a
    downstream sink can style and count them faithfully, matching what the engine
    records durably in the run's error log.
    """

    OK = "ok"
    SKIPPED_NO_CITATION = "skipped_no_citation"
    ERROR = "error"
    DEFERRED = "deferred"


class WaitReason(StrEnum):
    """Why the engine is deliberately waiting (the thing the old bar hid)."""

    POLITENESS = "politeness"
    RETRY_BACKOFF = "retry_backoff"
    OUTAGE_PROBE = "outage_probe"


@dataclass(frozen=True, slots=True)
class RunStarted:
    """A run began; carries the up-front estimate used for the overall bar/ETA."""

    run_name: str
    courts: tuple[str, ...]
    phases: tuple[str, ...]
    est_total_requests: int


@dataclass(frozen=True, slots=True)
class PhaseStarted:
    """A crawl phase began (``index``/``total`` are 1-based for display)."""

    phase: str
    index: int
    total: int
    items: int


@dataclass(frozen=True, slots=True)
class PhaseFinished:
    """A crawl phase finished; ``counts`` is a snapshot of the run's tallies."""

    phase: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class ItemStarted:
    """Work began on one item; ``label`` is a citation/title for the "Now" line."""

    phase: str
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class ItemFinished:
    """Work on one item finished with a distinct :class:`ItemStatus`."""

    phase: str
    status: ItemStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class WaitStarted:
    """The engine is about to block for ``seconds`` (politeness/backoff/probe).

    ``until_monotonic`` is ``time.monotonic()`` at the moment the wait ends, so a
    render thread can animate a live countdown against its own clock without any
    further events during the (blocking) sleep.
    """

    reason: WaitReason
    seconds: float
    until_monotonic: float


@dataclass(frozen=True, slots=True)
class RequestStarted:
    """A real network request is starting *now* (clears any pending countdown).

    Emitted from the HTTP layer after the politeness sleep returns, so the
    dashboard stops showing "next request in Ns" the instant the request begins
    rather than freezing at ``0.0s`` for the duration of the call.
    """

    url: str


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """A transient request failed and a backoff sleep is about to happen."""

    url: str
    attempt: int
    max_attempts: int
    wait_seconds: float


@dataclass(frozen=True, slots=True)
class OutagePaused:
    """The site is assumed down after ``consecutive`` consecutive failures."""

    consecutive: int


@dataclass(frozen=True, slots=True)
class OutageProbing:
    """Waiting ``probe_in_seconds`` before the next liveness probe."""

    down_seconds: float
    probe_in_seconds: float


@dataclass(frozen=True, slots=True)
class OutageRecovered:
    """The site responded again after ``down_seconds`` of downtime."""

    down_seconds: float


@dataclass(frozen=True, slots=True)
class OutageGaveUp:
    """The outage outlasted the cap; the phase stops for a later resume."""

    down_seconds: float


@dataclass(frozen=True, slots=True)
class Cancelled:
    """The user cancelled (first Ctrl-C); the current phase stops cleanly."""

    phase: str


@dataclass(frozen=True, slots=True)
class RunFinished:
    """The run ended; ``incomplete`` marks a run with outstanding work."""

    counts: dict[str, int]
    elapsed_seconds: float
    incomplete: bool


#: Union of every progress event a reporter may receive.
Event = (
    RunStarted
    | PhaseStarted
    | PhaseFinished
    | ItemStarted
    | ItemFinished
    | WaitStarted
    | RequestStarted
    | RetryScheduled
    | OutagePaused
    | OutageProbing
    | OutageRecovered
    | OutageGaveUp
    | Cancelled
    | RunFinished
)
