"""The reporter protocol and the null sink.

A :class:`ProgressReporter` receives every :mod:`~courts_scraper.progress.events`
event the engine emits and is a context manager so a live sink can set up and
tear down its terminal display around the run.

:class:`QuietReporter` is the null sink used for ``--quiet`` and as the default
when no reporter is supplied (so calling engine functions in a test or a library
context needs no wiring). It intentionally does nothing: the durable error log is
written by the engine itself (``_append_error``), so silencing the chrome never
loses the error trail.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, runtime_checkable

from courts_scraper.progress.events import Event


@runtime_checkable
class ProgressReporter(Protocol):
    """Sink for progress events; a context manager around the run."""

    def emit(self, event: Event) -> None:
        """Handle one progress event."""
        ...

    def __enter__(self) -> ProgressReporter:
        """Start the sink (e.g. begin a live display)."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the sink (e.g. restore the terminal), even on exception."""
        ...


class QuietReporter:
    """A reporter that discards every event (``--quiet`` and the default)."""

    def emit(self, event: Event) -> None:
        """Discard ``event``."""

    def __enter__(self) -> QuietReporter:
        """Return self; nothing to start."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Nothing to tear down."""
