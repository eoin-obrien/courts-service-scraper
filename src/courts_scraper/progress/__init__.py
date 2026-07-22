"""Progress reporting: typed events, a reporter protocol, and render sinks.

The crawl engine (:mod:`courts_scraper.run`) does not write to a console directly.
Instead it *emits* typed :mod:`~courts_scraper.progress.events` to a
:class:`~courts_scraper.progress.reporter.ProgressReporter`, and a render sink
turns those events into output -- a live terminal dashboard, a plain line log for
non-interactive runs, or nothing at all under ``--quiet``.

This split keeps ``rich`` out of the engine, makes the "deliberate waiting"
(politeness spacing, retry backoff, outage pauses) legible, and lets the same
event stream drive very different surfaces.
"""

from __future__ import annotations

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
    PhaseFinished,
    PhaseStarted,
    RequestStarted,
    RetryScheduled,
    RunFinished,
    RunStarted,
    WaitReason,
    WaitStarted,
)
from courts_scraper.progress.model import ProgressModel, Snapshot
from courts_scraper.progress.reporter import ProgressReporter, QuietReporter

__all__ = [
    "Cancelled",
    "Event",
    "ItemFinished",
    "ItemStarted",
    "ItemStatus",
    "OutageGaveUp",
    "OutagePaused",
    "OutageProbing",
    "OutageRecovered",
    "PhaseFinished",
    "PhaseStarted",
    "ProgressModel",
    "ProgressReporter",
    "QuietReporter",
    "RequestStarted",
    "RetryScheduled",
    "RunFinished",
    "RunStarted",
    "Snapshot",
    "WaitReason",
    "WaitStarted",
]
