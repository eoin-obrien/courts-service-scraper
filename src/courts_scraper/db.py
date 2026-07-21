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
from datetime import UTC, datetime
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

# Schema version, stamped in the DB via ``PRAGMA user_version``. Bump whenever a
# column or table is added below so a reader can tell a migrated DB from a fresh one.
# v3 adds the append-only ``pdf_version`` history table and ``last_revalidated_at``.
SCHEMA_VERSION = 3

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

-- Append-only per-document PDF version history. One row per distinct set of
-- bytes ever served for a record; ``superseded_at IS NULL`` marks the current
-- version. Populated by ``update --revalidate``: the row that exists here for a
-- ``done`` record preserves the audit trail (and, via ``filename``, where the
-- archived bytes live) that in-place mutation would otherwise erase. Kept in the
-- DB (not a sidecar log) so it exports and rides BagIt fixity like every column.
CREATE TABLE IF NOT EXISTS pdf_version (
    id                  INTEGER PRIMARY KEY,
    record_id           INTEGER NOT NULL,
    document_uuid       TEXT    NOT NULL DEFAULT '',
    pdf_url             TEXT    NOT NULL,
    neutral_citation    TEXT,
    filename            TEXT,
    sha256              TEXT    NOT NULL,
    bytes               INTEGER NOT NULL,
    fetched_at          TEXT    NOT NULL,
    superseded_at       TEXT,
    http_last_modified  TEXT,
    http_etag           TEXT,
    http_content_length INTEGER,
    http_content_type   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pdf_version_record ON pdf_version(record_id);
CREATE INDEX IF NOT EXISTS idx_pdf_version_current
    ON pdf_version(record_id, superseded_at);
"""

# Columns added after the v1 schema above. They live ONLY here -- not in the
# CREATE TABLE -- so a single migrate-on-open path brings both a fresh DB and an
# old run's DB to the current shape. Names are trusted constants (never user
# input), so interpolating them into ``ALTER TABLE`` is safe. Provenance is
# captured per observation phase: ``listed_at`` (search row seen),
# ``meta_retrieved_at`` (view page scraped), ``pdf_retrieved_at`` (binary
# fetched + verified), plus the PDF response's caching/type headers.
_ADDED_COLUMNS: dict[str, str] = {
    "listed_at": "TEXT",
    "meta_retrieved_at": "TEXT",
    "pdf_retrieved_at": "TEXT",
    "http_last_modified": "TEXT",
    "http_etag": "TEXT",
    "http_content_length": "INTEGER",
    "http_content_type": "TEXT",
    # Last time ``update --revalidate`` re-checked this row's bytes (independent of
    # ``pdf_retrieved_at``, which only moves when the bytes actually change). Drives
    # ``ORDER BY last_revalidated_at NULLS FIRST`` so revalidate is resumable after
    # an outage/cancel and ``--limit`` rotates through the corpus instead of always
    # re-checking the oldest N.
    "last_revalidated_at": "TEXT",
}


def _utcnow_iso() -> str:
    """Return the current time as a UTC ISO 8601 string (per-record provenance)."""
    return datetime.now(UTC).isoformat()


# The v1 ``record`` columns, in declaration order (see ``_SCHEMA``). Kept explicit
# so the read-only reader knows the full expected shape without opening a DB; a
# drift-guard test asserts this matches a freshly-created table.
_BASE_COLUMNS: tuple[str, ...] = (
    "id",
    "page",
    "title",
    "court",
    "judge",
    "date_delivered",
    "date_uploaded",
    "view_url",
    "pdf_url",
    "collection_uuid",
    "document_uuid",
    "neutral_citation",
    "record_number",
    "status_field",
    "result",
    "composition",
    "meta_json",
    "filename",
    "sha256",
    "bytes",
    "meta_status",
    "download_status",
    "error_reason",
)

# The full current column set (base + provenance). The read path fills any of
# these absent from an older DB with ``None`` instead of migrating the file.
RECORD_COLUMNS: tuple[str, ...] = _BASE_COLUMNS + tuple(_ADDED_COLUMNS)


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a run DB read-only for the export/corpus read paths.

    Unlike :class:`Repository`, this never migrates or writes, so reading an
    archived run leaves its bytes (and any recorded fixity) untouched and works on
    read-only media. Callers must tolerate columns absent from an older schema by
    defaulting them to ``None`` (see :data:`RECORD_COLUMNS`).
    """
    if not db_path.exists():
        # mode=ro cannot create a DB, so surface a clear error instead of a raw
        # sqlite OperationalError for a missing/pruned run folder.
        raise FileNotFoundError(f"no database at {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_pdf_versions(db_path: Path) -> list[sqlite3.Row]:
    """Read a run's full ``pdf_version`` history read-only, for the corpus snapshot.

    Returns an empty list when the run predates the history table (older schema),
    so the corpus reader can treat "no history" and "no changes" uniformly without
    migrating the archived run. Rows are ordered oldest-first per record.
    """
    conn = open_readonly(db_path)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pdf_version'"
        ).fetchone()
        if not has_table:
            return []
        return conn.execute(
            "SELECT * FROM pdf_version ORDER BY record_id, fetched_at, id"
        ).fetchall()
    except sqlite3.DatabaseError:
        # A corrupt/locked run DB must not abort a whole corpus build over the
        # optional revision history; treat it as "no history" (the missing-DB case).
        return []
    finally:
        conn.close()


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
        # Wait briefly rather than failing instantly if another process holds the
        # write lock (e.g. an overlapping run); the run-level file lock is the
        # primary guard, this is a cheap second line against a transient clash.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring the ``record`` table up to :data:`SCHEMA_VERSION` in place.

        Idempotent: adds any column from :data:`_ADDED_COLUMNS` that a DB created
        by an older version is missing (backfilled ``NULL``), then stamps
        ``user_version``. Runs on every open, so an archived run folder keeps
        working without a re-scrape, and a fresh DB reaches the same shape.
        """
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(record)")}
        for name, decl in _ADDED_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE record ADD COLUMN {name} {decl}")
        # Never down-stamp: a DB written by a future version keeps its higher
        # user_version, so the "newer than us" signal is not silently erased.
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
                view_url, pdf_url, collection_uuid, document_uuid, listed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # First-seen time. ON CONFLICT DO NOTHING means a re-listing keeps
                # the original ``listed_at`` rather than resetting it.
                _utcnow_iso(),
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
                meta_status = ?, meta_retrieved_at = ?, error_reason = NULL
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
                _utcnow_iso(),
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
    def record_download(
        self,
        record_id: int,
        sha256: str,
        size: int,
        *,
        last_modified: str | None = None,
        etag: str | None = None,
        content_length: int | None = None,
        content_type: str | None = None,
    ) -> None:
        """Mark a record's PDF as downloaded and verified, with fetch provenance.

        ``sha256``/``size`` are the verified digest and on-disk byte count. The
        keyword args carry the PDF response's caching/type headers as served, for
        provenance; any the server omits are stored ``NULL``. ``pdf_retrieved_at``
        is stamped now.
        """
        self._conn.execute(
            """
            UPDATE record SET
                sha256 = ?, bytes = ?, download_status = ?,
                pdf_retrieved_at = ?, http_last_modified = ?, http_etag = ?,
                http_content_length = ?, http_content_type = ?,
                error_reason = NULL
            WHERE id = ?
            """,
            (
                sha256,
                size,
                DL_DONE,
                _utcnow_iso(),
                last_modified,
                etag,
                content_length,
                content_type,
                record_id,
            ),
        )
        self._conn.commit()

    def record_download_error(self, record_id: int, reason: str) -> None:
        """Flag a record's PDF download as errored."""
        self._conn.execute(
            "UPDATE record SET download_status = ?, error_reason = ? WHERE id = ?",
            (DL_ERROR, reason, record_id),
        )
        self._conn.commit()

    # -- revalidation: per-document version history -----------------------
    def backfill_pdf_versions(self) -> int:
        """Seed a *current* ``pdf_version`` row for every ``done`` record missing one.

        Runs are created before the history table existed and downloads never
        wrote to it, so the first revalidation must record each document's
        already-fetched bytes as its version 1 (``filename`` = the live PDF name).
        Idempotent: a record that already has any version row is left untouched, so
        repeated calls (every revalidate) add nothing. Returns rows inserted.
        """
        rows = self._conn.execute(
            """
            SELECT r.* FROM record r
            WHERE r.download_status = ? AND r.sha256 IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM pdf_version v WHERE v.record_id = r.id
              )
            """,
            (DL_DONE,),
        ).fetchall()
        for r in rows:
            self._insert_version(
                record_id=r["id"],
                row=r,
                filename=r["filename"],
                sha256=str(r["sha256"]),
                size=int(r["bytes"] or 0),
                fetched_at=str(r["pdf_retrieved_at"] or _utcnow_iso()),
                last_modified=r["http_last_modified"],
                etag=r["http_etag"],
                content_length=r["http_content_length"],
                content_type=r["http_content_type"],
            )
        self._conn.commit()
        return len(rows)

    def iter_revalidation_targets(
        self, *, fetched_before: str | None = None
    ) -> Iterator[sqlite3.Row]:
        """Yield fully-downloaded records to re-check, least-recently-checked first.

        Ordering by ``last_revalidated_at`` (NULLs, i.e. never-checked, first) makes
        a ``--limit`` sweep rotate across the corpus and lets a run resume roughly
        where an outage/cancel stopped, rather than always re-checking the oldest ids.
        ``sha256 IS NOT NULL`` is required so the stored digest we compare against
        (and archive the old bytes under) is always real -- matching what
        :meth:`backfill_pdf_versions` seeds. ``fetched_before`` excludes rows fetched
        at/after that time, so a combined ``update --revalidate`` does not immediately
        re-download the judgments it just fetched in the same run.
        """
        if fetched_before is None:
            yield from self._conn.execute(
                """
                SELECT * FROM record
                WHERE meta_status = ? AND download_status = ? AND sha256 IS NOT NULL
                ORDER BY (last_revalidated_at IS NOT NULL), last_revalidated_at, id
                """,
                (META_OK, DL_DONE),
            )
        else:
            yield from self._conn.execute(
                """
                SELECT * FROM record
                WHERE meta_status = ? AND download_status = ? AND sha256 IS NOT NULL
                  AND (pdf_retrieved_at IS NULL OR pdf_retrieved_at < ?)
                ORDER BY (last_revalidated_at IS NOT NULL), last_revalidated_at, id
                """,
                (META_OK, DL_DONE, fetched_before),
            )

    def mark_revalidated(self, record_id: int) -> None:
        """Stamp a record as re-checked now without changing its bytes (unchanged)."""
        self._conn.execute(
            "UPDATE record SET last_revalidated_at = ? WHERE id = ?",
            (_utcnow_iso(), record_id),
        )
        self._conn.commit()

    def record_new_version(
        self,
        record_id: int,
        sha256: str,
        size: int,
        *,
        archived_filename: str,
        old_sha256: str,
        old_bytes: int,
        last_modified: str | None = None,
        etag: str | None = None,
        content_length: int | None = None,
        content_type: str | None = None,
    ) -> None:
        """Record that a re-checked document changed, preserving its prior version.

        In one transaction: mark the current ``pdf_version`` row superseded (moving
        its ``filename`` to ``archived_filename`` -- where the caller has archived
        the old bytes -- and stamping the *actual* archived ``old_sha256``/
        ``old_bytes``, which reconciles a stale current-version digest left by an
        earlier torn write), insert the new current version under the live filename,
        and flip the ``record`` row's digest/size/provenance and
        ``last_revalidated_at``. The caller owns the on-disk archive + atomic publish
        ordering; this method only makes the DB agree with disk.
        """
        now = _utcnow_iso()
        row = self._conn.execute(
            "SELECT * FROM record WHERE id = ?", (record_id,)
        ).fetchone()
        self._conn.execute(
            """
            UPDATE pdf_version SET superseded_at = ?, filename = ?,
                sha256 = ?, bytes = ?
            WHERE record_id = ? AND superseded_at IS NULL
            """,
            (now, archived_filename, old_sha256, old_bytes, record_id),
        )
        self._insert_version(
            record_id=record_id,
            row=row,
            filename=row["filename"],
            sha256=sha256,
            size=size,
            fetched_at=now,
            last_modified=last_modified,
            etag=etag,
            content_length=content_length,
            content_type=content_type,
        )
        self._conn.execute(
            """
            UPDATE record SET
                sha256 = ?, bytes = ?, pdf_retrieved_at = ?,
                http_last_modified = ?, http_etag = ?, http_content_length = ?,
                http_content_type = ?, last_revalidated_at = ?, error_reason = NULL
            WHERE id = ?
            """,
            (
                sha256,
                size,
                now,
                last_modified,
                etag,
                content_length,
                content_type,
                now,
                record_id,
            ),
        )
        self._conn.commit()

    def _insert_version(
        self,
        *,
        record_id: int,
        row: sqlite3.Row,
        filename: str | None,
        sha256: str,
        size: int,
        fetched_at: str,
        last_modified: str | None,
        etag: str | None,
        content_length: int | None,
        content_type: str | None,
    ) -> None:
        """Insert one ``pdf_version`` row (current; ``superseded_at`` left NULL)."""
        self._conn.execute(
            """
            INSERT INTO pdf_version (
                record_id, document_uuid, pdf_url, neutral_citation, filename,
                sha256, bytes, fetched_at, http_last_modified, http_etag,
                http_content_length, http_content_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                row["document_uuid"],
                row["pdf_url"],
                row["neutral_citation"],
                filename,
                sha256,
                size,
                fetched_at,
                last_modified,
                etag,
                content_length,
                content_type,
            ),
        )

    def count_revisions(self) -> int:
        """Count superseded versions (i.e. detected content changes) in this run."""
        return self._scalar(
            "SELECT COUNT(*) FROM pdf_version WHERE superseded_at IS NOT NULL"
        )

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
