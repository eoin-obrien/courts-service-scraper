"""SQLite persistence for a scraping run.

The schema is a single denormalised ``record`` table -- one row per judgment
document (i.e. per search-result row). Denormalisation is deliberate: a research
archive benefits from a flat table that exports straight to CSV, and each row is
self-describing. Progress is tracked with two independent status columns so a
run can resume exactly where it stopped:

* ``meta_status``     -- ``pending`` -> ``ok`` | ``error``  (view-page scrape)
* ``download_status`` -- ``pending`` -> ``done`` | ``error`` (PDF download)

Only a row with ``download_status = 'done'`` (and a verified file on disk) is
skipped on resume; everything else is retried.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

from courts_scraper.models import JudgmentMeta, ListRow

# Status vocabulary -------------------------------------------------------
META_PENDING = "pending"
META_OK = "ok"
META_ERROR = "error"

DL_PENDING = "pending"
DL_DONE = "done"
DL_ERROR = "error"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS record (
    id               INTEGER PRIMARY KEY,
    page             INTEGER NOT NULL,
    title            TEXT    NOT NULL,
    court            TEXT    NOT NULL,
    judge            TEXT    NOT NULL DEFAULT '',
    date_delivered   TEXT,
    date_uploaded    TEXT,
    view_url         TEXT    NOT NULL,
    pdf_url          TEXT    NOT NULL UNIQUE,
    collection_uuid  TEXT    NOT NULL DEFAULT '',
    document_uuid    TEXT    NOT NULL DEFAULT '',
    neutral_citation TEXT,
    record_number    TEXT,
    status_field     TEXT,
    result           TEXT,
    composition      TEXT,
    meta_json        TEXT,
    filename         TEXT,
    sha256           TEXT,
    bytes            INTEGER,
    meta_status      TEXT    NOT NULL DEFAULT 'pending',
    download_status  TEXT    NOT NULL DEFAULT 'pending',
    error_reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_meta_status     ON record(meta_status);
CREATE INDEX IF NOT EXISTS idx_download_status ON record(download_status);
CREATE INDEX IF NOT EXISTS idx_collection      ON record(collection_uuid);
"""


class Repository:
    """Thin data-access layer over the run's SQLite database.

    Usable as a context manager; the connection is committed and closed on exit.
    """

    def __init__(self, db_path: Path) -> None:
        """Open (creating if needed) the database at ``db_path``."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> Repository:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Commit and close the underlying connection."""
        self._conn.commit()
        self._conn.close()

    # -- phase 1: listing -------------------------------------------------
    def upsert_listing(self, row: ListRow) -> None:
        """Insert a listing row, ignoring rows already present (idempotent).

        Uniqueness is keyed on ``pdf_url`` so re-running the listing phase never
        duplicates rows or resets progress already made on existing ones.
        """
        self._conn.execute(
            """
            INSERT INTO record (
                page, title, court, judge, date_delivered, date_uploaded,
                view_url, pdf_url, collection_uuid, document_uuid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pdf_url) DO NOTHING
            """,
            (
                row.page,
                row.title,
                row.court,
                row.judge,
                row.date_delivered,
                row.date_uploaded,
                row.view_url,
                row.pdf_url,
                row.collection_uuid,
                row.document_uuid,
            ),
        )
        self._conn.commit()

    # -- phase 2: metadata ------------------------------------------------
    def record_metadata(
        self, record_id: int, meta: JudgmentMeta, filename: str
    ) -> None:
        """Store scraped metadata and the computed filename for a record."""
        self._conn.execute(
            """
            UPDATE record SET
                neutral_citation = ?, record_number = ?, status_field = ?,
                result = ?, composition = ?, meta_json = ?, filename = ?,
                meta_status = ?, error_reason = NULL
            WHERE id = ?
            """,
            (
                meta.neutral_citation,
                meta.fields.get("Record Number"),
                meta.fields.get("Status"),
                meta.fields.get("Result"),
                meta.fields.get("Composition of the Court"),
                json.dumps(_meta_to_json(meta), ensure_ascii=False),
                filename,
                META_OK,
                record_id,
            ),
        )
        self._conn.commit()

    def record_meta_error(
        self, record_id: int, reason: str, meta: JudgmentMeta | None = None
    ) -> None:
        """Flag a record's metadata as errored (e.g. missing citation).

        The raw metadata, if any, is still stored for manual follow-up.
        """
        meta_json = (
            json.dumps(_meta_to_json(meta), ensure_ascii=False) if meta else None
        )
        self._conn.execute(
            """
            UPDATE record SET meta_status = ?, error_reason = ?, meta_json = ?
            WHERE id = ?
            """,
            (META_ERROR, reason, meta_json, record_id),
        )
        self._conn.commit()

    # -- phase 2: download ------------------------------------------------
    def record_download(self, record_id: int, sha256: str, size: int) -> None:
        """Mark a record's PDF as successfully downloaded and verified."""
        self._conn.execute(
            """
            UPDATE record SET
                sha256 = ?, bytes = ?, download_status = ?, error_reason = NULL
            WHERE id = ?
            """,
            (sha256, size, DL_DONE, record_id),
        )
        self._conn.commit()

    def record_download_error(self, record_id: int, reason: str) -> None:
        """Flag a record's PDF download as errored."""
        self._conn.execute(
            "UPDATE record SET download_status = ?, error_reason = ? WHERE id = ?",
            (DL_ERROR, reason, record_id),
        )
        self._conn.commit()

    # -- queries ----------------------------------------------------------
    def iter_pending_metadata(self) -> Iterator[sqlite3.Row]:
        """Yield records whose view page has not yet been scraped."""
        yield from self._conn.execute(
            "SELECT * FROM record WHERE meta_status = ? ORDER BY id", (META_PENDING,)
        )

    def iter_pending_downloads(self) -> Iterator[sqlite3.Row]:
        """Yield records ready to download (metadata OK, not yet done)."""
        yield from self._conn.execute(
            """
            SELECT * FROM record
            WHERE meta_status = ? AND download_status IN (?, ?)
            ORDER BY id
            """,
            (META_OK, DL_PENDING, DL_ERROR),
        )

    def taken_filenames(self) -> set[str]:
        """Return the set of filenames already assigned (for collision checks)."""
        cur = self._conn.execute(
            "SELECT filename FROM record WHERE filename IS NOT NULL"
        )
        return {r["filename"] for r in cur}

    def counts(self) -> dict[str, int]:
        """Return a summary of row counts by status for the ``status`` command."""
        return {
            "total": self._scalar("SELECT COUNT(*) FROM record"),
            "meta_pending": self._count("meta_status", META_PENDING),
            "meta_ok": self._count("meta_status", META_OK),
            "meta_error": self._count("meta_status", META_ERROR),
            "download_pending": self._count("download_status", DL_PENDING),
            "download_done": self._count("download_status", DL_DONE),
            "download_error": self._count("download_status", DL_ERROR),
        }

    # Only these status columns may be counted; the value is always bound as a
    # parameter, and this allowlist ensures the interpolated column name can
    # never originate from user input.
    _COUNTABLE_COLUMNS = frozenset({"meta_status", "download_status"})

    def _count(self, column: str, value: str) -> int:
        if column not in self._COUNTABLE_COLUMNS:
            raise ValueError(f"not a countable column: {column!r}")
        return self._scalar(f"SELECT COUNT(*) FROM record WHERE {column} = ?", (value,))

    def _scalar(self, sql: str, params: tuple[str, ...] = ()) -> int:
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0


def _meta_to_json(meta: JudgmentMeta | None) -> dict[str, object]:
    """Serialise :class:`JudgmentMeta` to a JSON-friendly dict."""
    if meta is None:
        return {}
    return {
        "neutral_citation": meta.neutral_citation,
        "fields": meta.fields,
        "supplementary": [{"label": d.label, "url": d.url} for d in meta.supplementary],
    }
