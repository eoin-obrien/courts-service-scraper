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

    @property
    def name(self) -> str:
        """The run folder's name."""
        return self.path.name

    @property
    def is_complete(self) -> bool:
        """Whether every listed PDF has been downloaded."""
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
            "path": str(self.path),
        }

    @property
    def summary(self) -> str:
        """A one-line human summary used in the picker and listings."""
        if not self.readable:
            return f"{self.name}  (unreadable database)"
        if self.is_complete:
            return f"{self.name}  (complete, {self.total} PDFs)"
        parts = [f"{self.done}/{self.total} downloaded"]
        if self.error:
            parts.append(f"{self.error} error(s)")
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


def _read_run(path: Path) -> RunInfo:
    courts: tuple[str, ...] = ()
    created: str | None = None
    manifest = path / "manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            courts = tuple(data.get("courts", []))
            created = data.get("created")
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
    )
