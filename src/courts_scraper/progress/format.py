"""Small, dependency-free formatters shared by the render sinks.

Kept in :mod:`courts_scraper.progress` (which nothing in the crawl engine's hot
path imports) so both the engine's estimates and the sinks can render durations
and finish times the same way without an import cycle.
"""

from __future__ import annotations

import time
from collections.abc import Callable


def format_duration(seconds: float) -> str:
    """Render a rough duration as ``"45s"``, ``"12m"``, or ``"1h 5m"``."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 30 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def format_clock(
    wall_timestamp: float,
    *,
    localtime: Callable[[float], time.struct_time] = time.localtime,
) -> str:
    """Render a wall-clock POSIX timestamp as a local ``HH:MM`` finish time."""
    tm = localtime(wall_timestamp)
    return f"{tm.tm_hour:02d}:{tm.tm_min:02d}"
