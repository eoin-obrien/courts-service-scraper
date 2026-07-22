import csv
import hashlib
import json
import sqlite3

import pytest

from courts_scraper import corpus
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
    listing=None,
):
    path.mkdir(parents=True, exist_ok=True)
    manifest = {"base_url": "https://ww2.courts.ie", "courts": ["Supreme Court"]}
    if listing is not None:
        manifest["listing"] = listing
    (path / "manifest.json").write_text(
        json.dumps(manifest),
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


def test_snapshot_listing_sources_are_authoritative(tmp_path):
    """Per-run 'sources' carries the truth; only exactly-correct aggregates ship."""
    full = {
        "complete": True,
        "truncated": False,
        "max_pages": None,
        "pages_fetched": 26,
        "pages_available": 26,
    }
    capped = {
        "complete": True,
        "truncated": True,
        "max_pages": 3,
        "pages_fetched": 3,
        "pages_available": 26,
    }
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", listing=full)
    r2 = _make_run(tmp_path / "run2", uuid="d2", sha="bbb", listing=capped)
    bag = tmp_path / "bag"
    build_corpus([r1, r2], bag, formats=("csv",))

    listing = json.loads((bag / "data" / "snapshot.json").read_text(encoding="utf-8"))[
        "listing"
    ]
    # No misleading corpus-level "complete/truncated" boolean is emitted.
    assert "truncated" not in listing
    assert "any_full_crawl" not in listing
    # Per-source truth survives, and the two correct aggregates hold.
    assert listing["sources"]["run1"]["truncated"] is False
    assert listing["sources"]["run2"]["truncated"] is True
    assert listing["all_verified"] is True
    assert listing["any_truncated"] is True


def test_snapshot_listing_all_full_sources_not_truncated(tmp_path):
    full = {
        "complete": True,
        "truncated": False,
        "max_pages": None,
        "pages_fetched": 26,
        "pages_available": 26,
    }
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", listing=full)
    bag = tmp_path / "bag"
    build_corpus([r1], bag, formats=("csv",))

    listing = json.loads((bag / "data" / "snapshot.json").read_text(encoding="utf-8"))[
        "listing"
    ]
    assert listing["all_verified"] is True
    assert listing["any_truncated"] is False


def test_snapshot_listing_unverified_source_not_flagged_truncated(tmp_path):
    """A pre-feature run with no listing block reads as unverified, never truncated."""
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")  # no listing block
    bag = tmp_path / "bag"
    build_corpus([r1], bag, formats=("csv",))

    listing = json.loads((bag / "data" / "snapshot.json").read_text(encoding="utf-8"))[
        "listing"
    ]
    assert listing["sources"]["run1"] == {"verified": False}
    assert listing["all_verified"] is False
    # Unverified is not full, but must NOT be reported as truncated either.
    assert listing["any_truncated"] is False


def test_missing_pdf_is_surfaced_not_hidden(tmp_path):
    # downloaded=True but the PDF file is not on disk -> reported missing.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", write_pdf=False)
    bag = tmp_path / "bag"
    result = build_corpus([r1], bag, formats=("csv",))
    assert result.missing_pdfs == ("f1.pdf",)


def test_unsafe_pdf_filename_is_skipped_not_escaped(tmp_path):
    # A tampered DB filename must never let the bag copy read/write outside dest.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="ok.pdf")
    conn = sqlite3.connect(r1 / "judgments.sqlite")
    conn.execute("UPDATE record SET filename = ?", ("../../escape.pdf",))
    conn.commit()
    conn.close()

    bag = tmp_path / "bag"
    result = build_corpus([r1], bag, formats=("csv",))

    assert result.missing_pdfs == ("../../escape.pdf",)  # surfaced, not copied
    assert not (tmp_path / "escape.pdf").exists()  # nothing escaped the bag


def test_dataset_jsonld_is_schema_org(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    bag = tmp_path / "bag"
    build_corpus([r1], bag, formats=("csv",))
    doc = json.loads((bag / "data" / "dataset.jsonld").read_text(encoding="utf-8"))
    assert doc["@type"] == "Dataset"
    assert doc["spatialCoverage"] == "Ireland"
    assert doc["variableMeasured"]  # non-empty variable list


def test_cross_run_filename_collision_gets_unique_bag_names(tmp_path):
    # Two DIFFERENT documents that slug to the same filename, different content.
    r1 = _make_run(
        tmp_path / "run1",
        uuid="d1",
        sha="aaa",
        filename="same.pdf",
        pdf_bytes=b"%PDF-1.7 AAA",
    )
    r2 = _make_run(
        tmp_path / "run2",
        uuid="d2",
        sha="bbb",
        filename="same.pdf",
        pdf_bytes=b"%PDF-1.7 BBB",
    )
    bag = tmp_path / "bag"
    result = build_corpus([r1, r2], bag, formats=("csv",))

    assert result.record_count == 2
    pdfs = sorted(p.name for p in (bag / "data" / "pdfs").glob("*.pdf"))
    assert len(pdfs) == 2  # neither silently overwrote the other
    contents = {(bag / "data" / "pdfs" / n).read_bytes() for n in pdfs}
    assert contents == {b"%PDF-1.7 AAA", b"%PDF-1.7 BBB"}
    # CSV filename column matches the bagged files exactly.
    with (bag / "data" / "judgments.csv").open(encoding="utf-8", newline="") as f:
        filenames = {row["filename"] for row in csv.DictReader(f)}
    assert filenames == set(pdfs)


def test_rebuild_cleans_stale_payload(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    bag = tmp_path / "bag"
    build_corpus([r1], bag, formats=("csv",))

    # Simulate leftovers from a prior, larger build.
    stale_pdf = bag / "data" / "pdfs" / "stale.pdf"
    stale_pdf.write_bytes(b"%PDF-1.7 stale")
    (bag / "data" / "OLD.txt").write_text("old", encoding="utf-8")

    build_corpus([r1], bag, formats=("csv",))  # rebuild must clean first

    assert not stale_pdf.exists()
    assert not (bag / "data" / "OLD.txt").exists()
    manifest = (bag / "manifest-sha256.txt").read_text(encoding="utf-8")
    assert "stale.pdf" not in manifest
    assert "OLD.txt" not in manifest


def test_empty_run_list_raises(tmp_path):
    with pytest.raises(ExportError, match="no runs"):
        build_corpus([], tmp_path / "bag")


# -- revision aggregation: streaming + bounded build ----------------------
def _insert_versions(
    run_dir, record_id, shas, *, uuid="d1", pdf_url="https://x/d1.pdf"
):
    """Insert a chain of pdf_version rows for one record (oldest->newest).

    Every row but the last is superseded and points at ``versions/<sha>.pdf``;
    consecutive pairs are the revisions ``_collect_revisions`` derives.
    """
    conn = sqlite3.connect(run_dir / "judgments.sqlite")
    last = len(shas) - 1
    for i, sha in enumerate(shas):
        superseded = None if i == last else f"2026-01-01T00:00:{i:02d}+00:00"
        filename = "live.pdf" if i == last else f"versions/{sha}.pdf"
        conn.execute(
            """
            INSERT INTO pdf_version (
                record_id, document_uuid, pdf_url, neutral_citation, filename,
                sha256, bytes, fetched_at, superseded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                uuid,
                pdf_url,
                "[2026] IESC 36",
                filename,
                sha,
                10,
                f"2026-01-01T00:00:{i:02d}+00:00",
                superseded,
            ),
        )
    conn.commit()
    conn.close()


def test_revision_build_is_bounded_but_count_is_true(tmp_path, monkeypatch):
    # Six versions of one document -> five distinct revisions. With the embed cap
    # lowered to three, the build must hold at most three entry dicts while still
    # counting all five: the cap bounds the *build*, not just serialization.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    _insert_versions(r1, 1, ["s0", "s1", "s2", "s3", "s4", "s5"])
    monkeypatch.setattr(corpus, "_MAX_SNAPSHOT_REVISIONS", 3)

    scan = corpus._collect_revisions([r1])

    assert scan.total == 5  # true count, not the sample size
    assert len(scan.entries) == 3  # never accumulated past the cap


def test_revision_scan_streams_via_iterator(tmp_path, monkeypatch):
    # The aggregation must read through the streaming reader (not a materialised
    # whole-table fetch), and still cap the embedded sample below the true total.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    _insert_versions(r1, 1, ["s0", "s1", "s2", "s3"])
    monkeypatch.setattr(corpus, "_MAX_SNAPSHOT_REVISIONS", 2)

    real_iter = corpus.iter_pdf_versions
    used = {"streamed": False}

    def spy(db_path):
        used["streamed"] = True
        yield from real_iter(db_path)

    monkeypatch.setattr(corpus, "iter_pdf_versions", spy)

    scan = corpus._collect_revisions([r1])

    assert used["streamed"]  # went through the streaming iterator
    assert scan.total == 3
    assert len(scan.entries) == 2


def test_snapshot_revisions_count_and_truncation(tmp_path, monkeypatch):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa")
    _insert_versions(r1, 1, ["s0", "s1", "s2", "s3"])
    monkeypatch.setattr(corpus, "_MAX_SNAPSHOT_REVISIONS", 2)
    bag = tmp_path / "bag"

    build_corpus([r1], bag, formats=("csv",))

    snap = json.loads((bag / "data" / "snapshot.json").read_text(encoding="utf-8"))
    assert snap["revisions"]["count"] == 3  # authoritative total
    assert len(snap["revisions"]["entries"]) == 2  # bounded sample
    assert snap["revisions"]["truncated"] is True


# -- version archive: only referenced, verified copies --------------------
def _archive_superseded(
    run_dir, record_id, *, old_bytes, archive_bytes=None, write=True
):
    """Record a superseded version and (optionally) place its archived file.

    Returns the content-addressed name ``<old_sha>.pdf``. Pass ``archive_bytes``
    that differ from ``old_bytes`` to simulate a corrupt archive; ``write=False``
    leaves the reference dangling (referenced but missing on disk).
    """
    old_sha = hashlib.sha256(old_bytes).hexdigest()
    _insert_versions(run_dir, record_id, [old_sha, "newlive"])
    if write:
        vdir = run_dir / "pdfs" / "versions"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / f"{old_sha}.pdf").write_bytes(
            archive_bytes if archive_bytes is not None else old_bytes
        )
    return f"{old_sha}.pdf"


def test_referenced_verified_version_is_bagged(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    name = _archive_superseded(r1, 1, old_bytes=b"%PDF-1.7 old bytes")
    bag = tmp_path / "bag"

    result = build_corpus([r1], bag, formats=("csv",))

    bagged = bag / "data" / "pdfs" / "versions" / name
    assert bagged.read_bytes() == b"%PDF-1.7 old bytes"
    assert result.unverified_versions == ()
    # It rides BagIt fixity like any payload file.
    manifest = (bag / "manifest-sha256.txt").read_text(encoding="utf-8")
    assert f"data/pdfs/versions/{name}" in manifest


def test_orphan_version_file_is_not_bagged(tmp_path):
    # A versions/*.pdf with no superseded row referencing it is noise -> excluded.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    vdir = r1 / "pdfs" / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    orphan_bytes = b"%PDF-1.7 orphan"
    orphan = f"{hashlib.sha256(orphan_bytes).hexdigest()}.pdf"
    (vdir / orphan).write_bytes(orphan_bytes)
    bag = tmp_path / "bag"

    result = build_corpus([r1], bag, formats=("csv",))

    assert not (bag / "data" / "pdfs" / "versions" / orphan).exists()
    assert result.unverified_versions == ()  # unreferenced, so not a gap


def test_corrupt_version_file_is_skipped_and_surfaced(tmp_path):
    # Referenced, but the archived bytes do not hash to the name -> never attested.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    name = _archive_superseded(
        r1, 1, old_bytes=b"%PDF-1.7 true", archive_bytes=b"%PDF-1.7 corrupt"
    )
    bag = tmp_path / "bag"

    result = build_corpus([r1], bag, formats=("csv",))

    assert not (bag / "data" / "pdfs" / "versions" / name).exists()
    assert result.unverified_versions == (name,)


def test_referenced_missing_version_is_surfaced(tmp_path):
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    name = _archive_superseded(r1, 1, old_bytes=b"%PDF-1.7 gone", write=False)
    bag = tmp_path / "bag"

    result = build_corpus([r1], bag, formats=("csv",))

    assert not (bag / "data" / "pdfs" / "versions" / name).exists()
    assert result.unverified_versions == (name,)


def test_version_read_error_surfaces_not_aborts(tmp_path, monkeypatch):
    # The archive exists and passes is_file(), but the read raises mid-build (a
    # concurrent prune / unreadable file). It must surface as a gap and leave the
    # whole bag build intact -- never propagate out of build_corpus.
    r1 = _make_run(tmp_path / "run1", uuid="d1", sha="aaa", filename="a.pdf")
    name = _archive_superseded(r1, 1, old_bytes=b"%PDF-1.7 vanishes")
    bag = tmp_path / "bag"

    real = corpus.sha256_of

    def flaky(path):
        if path.name == name:  # only the referenced archive read; manifest still hashes
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(corpus, "sha256_of", flaky)

    result = build_corpus([r1], bag, formats=("csv",))  # must not raise

    assert result.unverified_versions == (name,)
    assert not (bag / "data" / "pdfs" / "versions" / name).exists()
    # The rest of the bag is still whole and fixity-valid.
    assert (bag / "manifest-sha256.txt").exists()
    assert (bag / "data" / "pdfs" / "a.pdf").exists()
