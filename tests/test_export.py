import csv
import importlib.util
import json

import pytest

from courts_scraper.dataset import iter_records
from courts_scraper.db import Repository
from courts_scraper.export import (
    _FIELD_NAMES,
    EXPORT_FIELDS,
    ExportError,
    _flat_row,
    export_run,
)
from courts_scraper.models import JudgmentMeta, ListRow


def _listing(pdf_url: str, judge: str, uuid: str) -> ListRow:
    return ListRow(
        page=0,
        title="O'Donnell -v- DCC",
        court="Supreme Court",
        judge=judge,
        date_delivered="2026-07-02",
        date_uploaded="2026-07-02",
        view_url=f"https://ww2.courts.ie/view/{uuid}",
        pdf_url=pdf_url,
        collection_uuid="col-1",
        document_uuid=uuid,
    )


@pytest.fixture
def run_dir(tmp_path):
    """A run folder with two opinions of one case (shared citation, distinct docs)."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"base_url": "https://ww2.courts.ie", "courts": ["Supreme Court"]}),
        encoding="utf-8",
    )
    with Repository(tmp_path / "judgments.sqlite") as repo:
        # Opinion 1: fully downloaded, in-vocab, with response provenance.
        repo.upsert_listing(_listing("https://x/a.pdf", "Woulfe J.", "d1"))
        (a,) = [r for r in repo.iter_pending_metadata() if r["document_uuid"] == "d1"]
        repo.record_metadata(
            a["id"],
            JudgmentMeta(
                neutral_citation="[2026] IESC 36",
                fields={
                    "Composition of the Court": "A C.J.;B J.",
                    "Status": "Approved",
                    "Result": "Allow Appeal",
                    "Record Number": "89/2025",
                },
            ),
            "2026_IESC_36_Woulfe-J.pdf",
        )
        repo.record_download(
            a["id"],
            sha256="deadbeef",
            size=1024,
            content_type="application/pdf",
            last_modified="Wed, 02 Jul 2026 09:00:00 GMT",
        )
        # Opinion 2: same citation, out-of-vocab status, no download yet.
        repo.upsert_listing(_listing("https://x/b.pdf", "Hogan J.", "d2"))
        (b,) = [r for r in repo.iter_pending_metadata() if r["document_uuid"] == "d2"]
        repo.record_metadata(
            b["id"],
            JudgmentMeta(
                neutral_citation="[2026] IESC 36",
                fields={"Composition of the Court": "A C.J.;B J.", "Status": "Draft"},
            ),
            "2026_IESC_36_Hogan-J.pdf",
        )
    return tmp_path


def _read_csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_flat_row_keys_match_field_names(run_dir):
    raw, derived = next(iter(iter_records(run_dir)))
    assert set(_flat_row(raw, derived)) == set(_FIELD_NAMES)


def test_export_writes_all_files(run_dir, tmp_path):
    out = tmp_path / "export"
    result = export_run(run_dir, out, formats=("csv", "json"))

    assert result.record_count == 2
    names = {p.name for p in result.files}
    assert names == {
        "judgments.csv",
        "judgments.json",
        "datapackage.json",
        "DATA_DICTIONARY.md",
    }
    for name in names:
        assert (out / name).exists()


def test_csv_header_and_values(run_dir, tmp_path):
    out = tmp_path / "export"
    export_run(run_dir, out, formats=("csv",))
    rows = _read_csv(out / "judgments.csv")

    assert rows[0] == list(_FIELD_NAMES)
    by_uuid = {r[0]: dict(zip(_FIELD_NAMES, r, strict=True)) for r in rows[1:]}

    d1 = by_uuid["d1"]
    assert d1["panel"] == "A C.J.;B J."
    assert d1["panel_count"] == "2"
    assert d1["status_in_vocab"] == "true"
    assert d1["vocab_flags"] == ""
    assert d1["sha256"] == "deadbeef"
    assert d1["http_content_type"] == "application/pdf"

    d2 = by_uuid["d2"]
    assert d2["status"] == "Draft"
    assert d2["status_in_vocab"] == "false"
    assert d2["vocab_flags"] != ""
    assert d2["result"] == ""  # None -> empty (missingValues)
    assert d2["sha256"] == ""  # not downloaded


def test_datapackage_descriptor(run_dir, tmp_path):
    out = tmp_path / "export"
    export_run(run_dir, out, formats=("csv",))
    pkg = json.loads((out / "datapackage.json").read_text(encoding="utf-8"))

    resource = pkg["resources"][0]
    assert resource["path"] == "judgments.csv"
    schema = resource["schema"]
    assert schema["primaryKey"] == "document_uuid"  # NOT neutral_citation
    assert schema["missingValues"] == [""]
    assert len(schema["fields"]) == len(EXPORT_FIELDS)
    assert pkg["coverage"]["record_count"] == 2
    assert pkg["coverage"]["date_delivered_min"] == "2026-07-02"
    assert pkg["courts"] == ["Supreme Court"]
    assert "public domain" in pkg["licenses"][0]["title"].lower()
    assert "reporting restrictions" in pkg["rights_note"]


def test_json_carries_nested_fields(run_dir, tmp_path):
    out = tmp_path / "export"
    export_run(run_dir, out, formats=("json",))
    records = json.loads((out / "judgments.json").read_text(encoding="utf-8"))

    d1 = next(r for r in records if r["document_uuid"] == "d1")
    assert d1["panel"] == ["A C.J.", "B J."]  # array, not ';'-joined
    assert d1["metadata_fields"]["Record Number"] == "89/2025"
    assert "supplementary" in d1


def test_neutral_citation_repeats_across_opinions(run_dir, tmp_path):
    # Two rows, one case: citation is non-unique -> must not be the primary key.
    out = tmp_path / "export"
    export_run(run_dir, out, formats=("csv",))
    rows = _read_csv(out / "judgments.csv")[1:]
    citations = [r[list(_FIELD_NAMES).index("neutral_citation")] for r in rows]
    assert citations == ["[2026] IESC 36", "[2026] IESC 36"]


def test_csv_formula_injection_is_neutralised(run_dir, tmp_path):
    import sqlite3

    conn = sqlite3.connect(run_dir / "judgments.sqlite")
    conn.execute(
        "UPDATE record SET title = ? WHERE document_uuid = 'd1'",
        ('=HYPERLINK("http://evil")',),
    )
    conn.commit()
    conn.close()

    out = tmp_path / "export"
    export_run(run_dir, out, formats=("csv",))
    rows = _read_csv(out / "judgments.csv")
    d1 = {r[0]: dict(zip(_FIELD_NAMES, r, strict=True)) for r in rows[1:]}["d1"]
    # Leading '=' neutralised so a spreadsheet renders it as text, not a formula.
    assert d1["title"].startswith("'=")


def test_descriptor_references_the_written_format(run_dir, tmp_path):
    out = tmp_path / "export"
    export_run(run_dir, out, formats=("json",))  # no CSV written
    pkg = json.loads((out / "datapackage.json").read_text(encoding="utf-8"))
    assert pkg["resources"][0]["path"] == "judgments.json"
    assert not (out / "judgments.csv").exists()  # descriptor must not point at it


def test_unknown_format_raises(run_dir, tmp_path):
    with pytest.raises(ExportError, match="unknown export format"):
        export_run(run_dir, tmp_path / "out", formats=("csv", "xlsx"))


@pytest.mark.skipif(
    importlib.util.find_spec("pyarrow") is None, reason="pyarrow not installed"
)
def test_parquet_written_when_pyarrow_present(run_dir, tmp_path):
    out = tmp_path / "export"
    result = export_run(run_dir, out, formats=("parquet",))
    assert (out / "judgments.parquet").exists()
    assert (out / "judgments.parquet") in result.files


@pytest.mark.skipif(
    importlib.util.find_spec("pyarrow") is not None, reason="pyarrow installed"
)
def test_parquet_without_pyarrow_gives_clean_error(run_dir, tmp_path):
    with pytest.raises(ExportError, match="pyarrow"):
        export_run(run_dir, tmp_path / "out", formats=("parquet",))
