"""Discovery of existing run folders for the resume experience.

Instead of hand-typing ``--run-dir``, the CLI can list the runs under a data
directory (each a folder holding a ``judgments.sqlite``) and show their
progress, so a run can be picked to resume.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from courts_scraper.db import Repository


@dataclass(frozen=True)
class RunInfo:
    """A discovered run folder and its progress."""

    path: Path
    courts: tuple[str, ...]
    created: str | None
    total: int
    done: int
    error: int
    readable: bool
    # Listing completeness, read from the manifest's ``listing`` block (absent on
    # pre-feature runs). ``listing_verified`` distinguishes "a listing pass finished
    # and this is its verdict" from "unknown" -- so a bare ``listing_truncated`` of
    # False is never mistaken for a *verified* full crawl.
    listing_verified: bool = False
    listing_truncated: bool = False
    pages_fetched: int | None = None
    pages_available: int | None = None

    @property
    def name(self) -> str:
        """The run folder's name."""
        return self.path.name

    @property
    def is_complete(self) -> bool:
        """Whether every listed PDF has been downloaded.

        Download-oriented on purpose: a run can have every *listed* PDF while its
        *listing* was truncated. Callers that need a canonical full crawl must also
        check :attr:`listing_truncated` (e.g. the duplicate-run guard in ``fetch``).
        """
        return self.readable and self.total > 0 and self.done >= self.total

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view for ``runs --json``."""
        return {
            "name": self.name,
            "courts": list(self.courts),
            "created": self.created,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "readable": self.readable,
            "complete": self.is_complete,
            "listing_verified": self.listing_verified,
            "listing_truncated": self.listing_truncated,
            "pages_fetched": self.pages_fetched,
            "pages_available": self.pages_available,
            "path": str(self.path),
        }

    @property
    def summary(self) -> str:
        """A one-line human summary used in the picker and listings."""
        if not self.readable:
            return f"{self.name}  (unreadable database)"
        if self.is_complete:
            if self.listing_truncated:
                span = _pages_span(self.pages_fetched, self.pages_available)
                return f"{self.name}  (downloads complete; listing truncated{span})"
            return f"{self.name}  (complete, {self.total} PDFs)"
        parts = [f"{self.done}/{self.total} downloaded"]
        if self.error:
            parts.append(f"{self.error} error(s)")
        if self.listing_truncated:
            parts.append("listing truncated")
        return f"{self.name}  ({', '.join(parts)})"


def list_runs(data_dir: Path) -> list[RunInfo]:
    """Return runs under ``data_dir``, newest first.

    A run is any immediate subdirectory containing a ``judgments.sqlite``.
    Folder names are timestamp-prefixed, so a reverse name sort is chronological.
    """
    if not data_dir.exists():
        return []
    runs = [
        _read_run(child)
        for child in sorted(data_dir.iterdir(), reverse=True)
        if (child / "judgments.sqlite").is_file()
    ]
    return runs


def latest_run(data_dir: Path) -> RunInfo | None:
    """Return the most recent run under ``data_dir``, or ``None`` if there are none."""
    runs = list_runs(data_dir)
    return runs[0] if runs else None


def _pages_span(fetched: int | None, available: int | None) -> str:
    """Render ``" at 3 of 27 pages"`` when the counts are known, else ``""``."""
    if fetched is None or available is None:
        return ""
    return f" at {fetched} of {available} pages"


def _listing_fields(data: dict[str, object]) -> dict[str, object]:
    """Parse the manifest's ``listing`` block defensively.

    A missing or malformed block reads as *unverified* (not "full"): only a block
    that explicitly recorded a finished pass sets ``listing_verified``.
    """
    block = data.get("listing")
    if not isinstance(block, dict) or block.get("complete") is not True:
        return {}
    fetched = block.get("pages_fetched")
    available = block.get("pages_available")
    return {
        "listing_verified": True,
        "listing_truncated": bool(block.get("truncated", False)),
        "pages_fetched": fetched if isinstance(fetched, int) else None,
        "pages_available": available if isinstance(available, int) else None,
    }


def _read_run(path: Path) -> RunInfo:
    courts: tuple[str, ...] = ()
    created: str | None = None
    listing: dict[str, object] = {}
    manifest = path / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            courts = tuple(data.get("courts", []))
            created = data.get("created")
            listing = _listing_fields(data)
        except (OSError, json.JSONDecodeError):
            pass

    total = done = error = 0
    readable = True
    try:
        with Repository(path / "judgments.sqlite") as repo:
            counts = repo.counts()
        total = counts["total"]
        done = counts["download_done"]
        error = counts["meta_error"] + counts["download_error"]
    except Exception:  # a broken or foreign DB is simply reported as unreadable
        readable = False

    return RunInfo(
        path=path,
        courts=courts,
        created=created,
        total=total,
        done=done,
        error=error,
        readable=readable,
        **listing,  # type: ignore[arg-type]
    )
