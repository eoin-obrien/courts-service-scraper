import sqlite3
from datetime import datetime

import pytest

from courts_scraper.db import _ADDED_COLUMNS, SCHEMA_VERSION, Repository
from courts_scraper.models import JudgmentMeta, ListRow

# The v1 record table, before any provenance columns -- used to fabricate an
# "old run" DB and prove migrate-on-open upgrades it in place.
_V1_SCHEMA = """
CREATE TABLE record (
    id INTEGER PRIMARY KEY, page INTEGER NOT NULL, title TEXT NOT NULL,
    court TEXT NOT NULL, judge TEXT NOT NULL DEFAULT '', date_delivered TEXT,
    date_uploaded TEXT, view_url TEXT NOT NULL, pdf_url TEXT NOT NULL UNIQUE,
    collection_uuid TEXT NOT NULL DEFAULT '', document_uuid TEXT NOT NULL DEFAULT '',
    neutral_citation TEXT, record_number TEXT, status_field TEXT, result TEXT,
    composition TEXT, meta_json TEXT, filename TEXT, sha256 TEXT, bytes INTEGER,
    meta_status TEXT NOT NULL DEFAULT 'pending',
    download_status TEXT NOT NULL DEFAULT 'pending', error_reason TEXT
);
"""


def _row(pdf_url: str, judge: str = "Woulfe J.", collection: str = "col-1") -> ListRow:
    return ListRow(
        page=0,
        title="O'Donnell -v- Dublin City Council",
        court="Supreme Court",
        judge=judge,
        date_delivered="2026-07-02",
        date_uploaded="2026-07-02",
        view_url=f"https://ww2.courts.ie/view/Judgments/doc/{collection}/x.pdf/pdf",
        pdf_url=pdf_url,
        collection_uuid=collection,
        document_uuid="doc-" + pdf_url[-1],
    )


@pytest.fixture
def repo(tmp_path):
    with Repository(tmp_path / "judgments.sqlite") as repository:
        yield repository


def test_upsert_is_idempotent(repo):
    row = _row("https://x/a.pdf")
    repo.upsert_listing(row)
    repo.upsert_listing(row)  # second insert must not duplicate
    assert repo.counts()["total"] == 1


def test_listing_then_metadata_then_download_flow(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())

    meta = JudgmentMeta(neutral_citation="[2026] IESC 36", fields={"Court": "Supreme"})
    repo.record_metadata(row["id"], meta, "2026_IESC_36_Woulfe-J.pdf")

    assert not list(repo.iter_pending_metadata())
    (ready,) = list(repo.iter_pending_downloads())
    assert ready["filename"] == "2026_IESC_36_Woulfe-J.pdf"

    repo.record_download(ready["id"], sha256="abc", size=123)
    # A completed download is not offered again -> resume skips it.
    assert not list(repo.iter_pending_downloads())
    counts = repo.counts()
    assert counts["download_done"] == 1


def test_meta_error_excludes_from_downloads(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    repo.record_meta_error(row["id"], "no_neutral_citation")
    assert not list(repo.iter_pending_downloads())
    assert repo.counts()["meta_error"] == 1


def test_download_error_is_retried(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    repo.record_metadata(
        row["id"], JudgmentMeta(neutral_citation="[2026] IESC 36"), "f.pdf"
    )
    (ready,) = list(repo.iter_pending_downloads())
    repo.record_download_error(ready["id"], "boom")
    # Errored downloads remain eligible for a retry on the next run.
    assert [r["id"] for r in repo.iter_pending_downloads()] == [ready["id"]]


def _columns(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(record)")}
    finally:
        conn.close()


def test_migrate_on_open_upgrades_legacy_db(tmp_path):
    # Fabricate a v1 DB (no provenance columns) with one existing row.
    db_path = tmp_path / "judgments.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO record (page, title, court, view_url, pdf_url) "
        "VALUES (0, 'Old Case', 'Supreme Court', 'https://x/v', 'https://x/a.pdf')"
    )
    conn.commit()
    conn.close()
    assert not (_ADDED_COLUMNS.keys() & _columns(db_path))  # none present yet

    # Opening it migrates in place: columns added, version stamped, row intact.
    with Repository(db_path) as repo:
        assert repo.counts()["total"] == 1
        version = repo._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert _ADDED_COLUMNS.keys() <= _columns(db_path)


def test_migrate_on_open_is_idempotent(tmp_path):
    db_path = tmp_path / "judgments.sqlite"
    with Repository(db_path):
        pass
    # Re-opening an already-current DB must not error or duplicate columns.
    with Repository(db_path) as repo:
        version = repo._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert list(_columns(db_path)).count("listed_at") == 1


def test_phase_timestamps_are_stamped(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    assert _is_iso(row["listed_at"])
    assert row["meta_retrieved_at"] is None  # not scraped yet

    repo.record_metadata(
        row["id"], JudgmentMeta(neutral_citation="[2026] IESC 36"), "f.pdf"
    )
    (ready,) = list(repo.iter_pending_downloads())
    assert _is_iso(ready["meta_retrieved_at"])
    assert ready["pdf_retrieved_at"] is None  # not downloaded yet

    repo.record_download(
        ready["id"],
        sha256="abc",
        size=123,
        last_modified="Wed, 02 Jul 2026 09:00:00 GMT",
        etag='"e1"',
        content_length=123,
        content_type="application/pdf",
    )
    done = repo._conn.execute(
        "SELECT * FROM record WHERE id = ?", (ready["id"],)
    ).fetchone()
    assert _is_iso(done["pdf_retrieved_at"])
    assert done["http_last_modified"] == "Wed, 02 Jul 2026 09:00:00 GMT"
    assert done["http_etag"] == '"e1"'
    assert done["http_content_length"] == 123
    assert done["http_content_type"] == "application/pdf"


def test_relisting_keeps_original_listed_at(repo):
    row = _row("https://x/a.pdf")
    repo.upsert_listing(row)
    first = next(iter(repo.iter_pending_metadata()))["listed_at"]
    repo.upsert_listing(row)  # ON CONFLICT DO NOTHING must not reset the stamp
    second = next(iter(repo.iter_pending_metadata()))["listed_at"]
    assert first == second


def _is_iso(value) -> bool:
    return isinstance(value, str) and datetime.fromisoformat(value) is not None


def test_taken_filenames(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    repo.record_metadata(
        row["id"],
        JudgmentMeta(neutral_citation="[2026] IESC 36"),
        "2026_IESC_36_Woulfe-J.pdf",
    )
    assert repo.taken_filenames() == {"2026_IESC_36_Woulfe-J.pdf"}
