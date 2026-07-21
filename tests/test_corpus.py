import hashlib
import json
import sqlite3

import pytest

from courts_scraper.corpus import build_corpus, merge_runs
from courts_scraper.db import Repository
from courts_scraper.export import ExportError
from courts_scraper.models import JudgmentMeta, ListRow


def _make_run(
    path,
    *,
    uuid,
    sha,
    filename="f1.pdf",
    pdf_bytes=b"%PDF-1.7 body",
    downloaded=True,
    write_pdf=True,
):
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(
        json.dumps({"base_url": "https://ww2.courts.ie", "courts": ["Supreme Court"]}),
        encoding="utf-8",
    )
    with Repository(path / "judgments.sqlite") as repo:
        repo.upsert_listing(
            ListRow(
                page=0,
                title="O'Donnell -v- DCC",
                court="Supreme Court",
                judge="Woulfe J.",
                date_delivered="2026-07-02",
                date_uploaded="2026-07-02",
                view_url=f"https://x/view/{uuid}",
                pdf_url=f"https://x/{uuid}.pdf",
                collection_uuid="col-1",
                document_uuid=uuid,
            )
        )
        (r,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            r["id"],
            JudgmentMeta(
                neutral_citation="[2026] IESC 36",
                fields={
                    "Composition of the Court": "A C.J.;B J.",
                    "Status": "Approved",
                    "Result": "Allow Appeal",
                },
            ),
            filename,
        )
        if downloaded:
            repo.record_download(r["id"], sha256=sha, size=len(pdf_bytes))
    if downloaded and write_pdf:
        (path / "pdfs").mkdir(exist_ok=True)
        (path / "pdfs" / filename).write_bytes(pdf_bytes)
    return path


def _backdate(path, ts):
    conn = sqlite3.connect(path / "judgments.sqlite")
    conn.execute("UPDATE record SET pdf_retrieved_at = ?", (ts,))
    conn.commit()
    conn.close()


# -- merge / dedup / conflict --------------------------------------------
def test_merge_dedups_by_document_uuid_latest_wins(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    _backdate(r1, "2020-01-01T00:00:00+00:00")  # force run1 older
    _make_run(tmp_path / "run2", uuid="d1", sha="bbb")

    merge = merge_runs([r1, tmp_path / "run2"])

    assert len(merge.pairs) == 1  # one document, deduped
    winner_raw = merge.pairs[0][0]
    assert winner_raw["sha256"] == "bbb"  # later run wins


def test_merge_surfaces_sha256_conflict(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    r2 = _make_run(tmp_path / "run2", uuid="d1", sha="bbb")

    merge = merge_runs([r1, r2])

    assert len(merge.conflicts) == 1
    conflict = merge.conflicts[0]
    assert conflict.document_uuid == "d1"
    assert conflict.sha256s == ("aaa", "bbb")
    assert set(conflict.runs) == {"run1", "run2"}


def test_merge_no_conflict_when_same_content(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="same")
    r2 = _make_run(tmp_path / "run2", uuid="d2", sha="same")  # different docs
    merge = merge_runs([r1, r2])
    assert merge.conflicts == []
    assert len(merge.pairs) == 2


# -- bag assembly ---------------------------------------------------------
def test_build_corpus_writes_a_valid_bag(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    r2 = _make_run(tmp_path / "run2", uuid="d2", sha="bbb", filename="b.pdf")
    bag = tmp_path / "bag"

    result = build_corpus([r1, r2], bag, formats=("csv", "json"))

    assert result.record_count == 2
    # BagIt structure
    for name in (
        "bagit.txt",
        "bag-info.txt",
        "manifest-sha256.txt",
        "tagmanifest-sha256.txt",
    ):
        assert (bag / name).exists()
    # Payload
    for name in (
        "judgments.csv",
        "judgments.json",
        "datapackage.json",
        "dataset.jsonld",
        "snapshot.json",
        "DATASHEET.md",
    ):
        assert (bag / "data" / name).exists()
    assert (bag / "data" / "pdfs" / "a.pdf").exists()
    assert (bag / "data" / "pdfs" / "b.pdf").exists()


def test_bag_manifest_hashes_match_payload(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    bag = tmp_path / "bag"
    build_corpus([r1], bag, formats=("csv",))

    manifest = (bag / "manifest-sha256.txt").read_text(encoding="utf-8")
    entries = dict(
        reversed(line.split("  ", 1)) for line in manifest.splitlines() if line
    )
    # Every listed file exists and its recorded digest is correct.
    for rel, digest in entries.items():
        path = bag / rel
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert "data/judgments.csv" in entries
    assert "data/pdfs/a.pdf" in entries


def test_snapshot_records_runs_and_conflicts(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    r2 = _make_run(tmp_path / "run2", uuid="d1", sha="bbb")
    bag = tmp_path / "bag"
    result = build_corpus([r1, r2], bag, formats=("csv",))

    snap = json.loads((bag / "data" / "snapshot.json").read_text(encoding="utf-8"))
    assert set(snap["source_runs"]) == {"run1", "run2"}
    assert snap["conflict_count"] == 1
    assert snap["record_count"] == 1
    assert snap["conflicts"][0]["document_uuid"] == "d1"
    assert len(result.conflicts) == 1


def test_missing_pdf_is_surfaced_not_hidden(tmp_path):
    # downloaded=True but the PDF file is not on disk -> reported missing.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", write_pdf=False)
    bag = tmp_path / "bag"
    result = build_corpus([r1], bag, formats=("csv",))
    assert result.missing_pdfs == ("f1.pdf",)


def test_dataset_jsonld_is_schema_org(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    bag = tmp_path / "bag"
    build_corpus([r1], bag, formats=("csv",))
    doc = json.loads((bag / "data" / "dataset.jsonld").read_text(encoding="utf-8"))
    assert doc["@type"] == "Dataset"
    assert doc["spatialCoverage"] == "Ireland"
    assert doc["variableMeasured"]  # non-empty variable list


def test_empty_run_list_raises(tmp_path):
    with pytest.raises(ExportError, match="no runs"):
        build_corpus([], tmp_path / "bag")
