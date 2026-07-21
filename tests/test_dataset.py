from courts_scraper.dataset import (
    RESULT_VOCAB,
    STATUS_VOCAB,
    Derived,
    _split_panel,
    derive,
    iter_records,
)
from courts_scraper.db import Repository
from courts_scraper.models import JudgmentMeta, ListRow


def _raw(**overrides) -> dict:
    row = {
        "judge": "Woulfe J.",
        "composition": "O'Donnell C.J.;Woulfe J.;Hogan J.;Murray J.;Donnelly J.",
        "status_field": "Approved",
        "result": "Allow Appeal",
    }
    row.update(overrides)
    return row


# -- panel splitting ------------------------------------------------------
def test_split_panel_normal():
    assert _split_panel("A C.J.;B J.;C J.") == ("A C.J.", "B J.", "C J.")


def test_split_panel_single():
    assert _split_panel("A J.") == ("A J.",)


def test_split_panel_none_and_empty():
    assert _split_panel(None) == ()
    assert _split_panel("") == ()


def test_split_panel_trailing_and_doubled_delimiters():
    assert _split_panel("A J.;;B J.;") == ("A J.", "B J.")


# -- author vs panel ------------------------------------------------------
def test_author_and_panel_are_separate():
    d = derive(_raw())
    # authoring_judge is THIS opinion's author; panel is the whole bench.
    assert d.authoring_judge == "Woulfe J."
    assert d.panel[0] == "O'Donnell C.J."
    assert "Woulfe J." in d.panel
    assert len(d.panel) == 5
    # The author is one of many on the panel -- never collapsed into it.
    assert d.authoring_judge != d.panel


def test_blank_author_becomes_empty_string():
    assert derive(_raw(judge="")).authoring_judge == ""
    assert derive(_raw(judge=None)).authoring_judge == ""


# -- controlled vocabularies (warn, never reject) -------------------------
def test_in_vocab_values_have_no_flags():
    d = derive(_raw())
    assert d.status_in_vocab and d.result_in_vocab
    assert d.flags == ()


def test_out_of_vocab_status_is_flagged_not_dropped():
    d = derive(_raw(status_field="Unapproved"))
    assert d.status == "Unapproved"  # emitted as-is
    assert d.status_in_vocab is False
    assert any("status" in f and "Unapproved" in f for f in d.flags)


def test_out_of_vocab_result_is_flagged():
    d = derive(_raw(result="Dismiss Appeal"))
    assert d.result == "Dismiss Appeal"
    assert d.result_in_vocab is False
    assert any("result" in f for f in d.flags)


def test_absent_status_is_not_drift():
    d = derive(_raw(status_field=None, result=None))
    assert d.status is None and d.status_in_vocab is True
    assert d.result is None and d.result_in_vocab is True
    assert d.flags == ()


def test_seed_vocab_matches_observed_values():
    # Guards against accidental vocab edits diverging from observed data.
    assert "Approved" in STATUS_VOCAB
    assert "Allow Appeal" in RESULT_VOCAB


# -- iter over a real run DB ----------------------------------------------
def test_iter_records_does_not_mutate_the_source_db(tmp_path):
    import hashlib

    with Repository(tmp_path / "judgments.sqlite") as repo:
        repo.upsert_listing(
            ListRow(
                page=0,
                title="X",
                court="Supreme Court",
                judge="Woulfe J.",
                date_delivered="2026-07-02",
                date_uploaded="2026-07-02",
                view_url="https://x/v",
                pdf_url="https://x/a.pdf",
                collection_uuid="c1",
                document_uuid="d1",
            )
        )
    db = tmp_path / "judgments.sqlite"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    list(iter_records(tmp_path))  # read path must not write
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert before == after


def test_iter_records_reads_legacy_db_with_null_provenance(tmp_path):
    # A v1 DB (no provenance columns) must read back full-shaped, no migration.
    import sqlite3

    db = tmp_path / "judgments.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE record (id INTEGER PRIMARY KEY, page INTEGER NOT NULL, "
        "title TEXT NOT NULL, court TEXT NOT NULL, judge TEXT NOT NULL DEFAULT '', "
        "date_delivered TEXT, date_uploaded TEXT, view_url TEXT NOT NULL, "
        "pdf_url TEXT NOT NULL UNIQUE, collection_uuid TEXT NOT NULL DEFAULT '', "
        "document_uuid TEXT NOT NULL DEFAULT '', neutral_citation TEXT, "
        "record_number TEXT, status_field TEXT, result TEXT, composition TEXT, "
        "meta_json TEXT, filename TEXT, sha256 TEXT, bytes INTEGER, "
        "meta_status TEXT NOT NULL DEFAULT 'pending', "
        "download_status TEXT NOT NULL DEFAULT 'pending', error_reason TEXT);"
    )
    conn.execute(
        "INSERT INTO record "
        "(page, title, court, view_url, pdf_url, judge, composition) "
        "VALUES (0, 'X', 'Supreme Court', 'https://x/v', 'https://x/a.pdf', "
        "'Woulfe J.', 'A C.J.;B J.')"
    )
    conn.commit()
    conn.close()

    (raw, derived) = next(iter(iter_records(tmp_path)))
    assert raw["pdf_retrieved_at"] is None  # provenance column absent -> None
    assert raw["http_etag"] is None
    assert derived.authoring_judge == "Woulfe J."  # existing columns still read
    assert derived.panel == ("A C.J.", "B J.")


def test_iter_records_derives_over_run_db(tmp_path):
    with Repository(tmp_path / "judgments.sqlite") as repo:
        repo.upsert_listing(
            ListRow(
                page=0,
                title="O'Donnell -v- DCC",
                court="Supreme Court",
                judge="Woulfe J.",
                date_delivered="2026-07-02",
                date_uploaded="2026-07-02",
                view_url="https://x/v",
                pdf_url="https://x/a.pdf",
                collection_uuid="c1",
                document_uuid="d1",
            )
        )
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"],
            JudgmentMeta(
                neutral_citation="[2026] IESC 36",
                fields={
                    "Composition of the Court": "A C.J.;B J.",
                    "Status": "Approved",
                    "Result": "Allow Appeal",
                },
            ),
            "f.pdf",
        )

    pairs = list(iter_records(tmp_path))
    assert len(pairs) == 1
    raw, d = pairs[0]
    assert raw["document_uuid"] == "d1"  # raw passthrough available
    assert isinstance(d, Derived)
    assert d.authoring_judge == "Woulfe J."
    assert d.panel == ("A C.J.", "B J.")
    assert d.status_in_vocab and d.flags == ()
