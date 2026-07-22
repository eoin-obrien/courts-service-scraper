"""Pick the right reporter for the current output surface.

One rule, applied once in the CLI: a wide interactive terminal gets the live
dashboard; ``--quiet`` gets the null sink; anything else -- a pipe, a cron job, a
CI log, or a terminal narrower than :data:`MIN_TERMINAL_WIDTH` -- gets the plain
line sink (never an animated bar smeared into a log). Non-interactive output goes
to ``stderr`` so a command's ``stdout`` stays clean for a piped consumer.
"""

from __future__ import annotations

import sys

from rich.console import Console

from courts_scraper.progress.dashboard import LiveDashboardReporter
from courts_scraper.progress.plain import PlainReporter
from courts_scraper.progress.reporter import ProgressReporter, QuietReporter

#: Minimum terminal width for the full multi-panel dashboard; below this the
#: plain single-line renderer is used instead.
MIN_TERMINAL_WIDTH = 80


def select_reporter(
    console: Console,
    *,
    quiet: bool,
    delay: float,
    jitter: float,
    prefer_plain: bool = False,
) -> ProgressReporter:
    """Choose a reporter for ``console``.

    Args:
        console: The engine's output console (stdout, or stderr in ``--json`` mode).
        quiet: Whether ``--quiet`` was requested.
        delay: Politeness delay, for the reporter's ETA.
        jitter: Politeness jitter.
        prefer_plain: Force the plain sink even on a wide terminal (used in
            ``--json`` mode so machine output on stdout is never fought by a live
            display).
    """
    if quiet:
        return QuietReporter()
    if not prefer_plain and console.is_terminal and console.width >= MIN_TERMINAL_WIDTH:
        return LiveDashboardReporter(console, delay=delay, jitter=jitter)
    stream = console.file if console.is_terminal else sys.stderr
    width = console.width if console.is_terminal else 200
    return PlainReporter(stream, delay=delay, jitter=jitter, width=width)
