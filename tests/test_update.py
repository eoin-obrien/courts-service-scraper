"""Tests for the ``update`` command: incremental re-list (Tier 1) and
revalidation with per-document version history (Tier 2)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import typer
from rich.console import Console

from courts_scraper.cli import _confirm_update
from courts_scraper.db import Repository, open_readonly, read_pdf_versions
from courts_scraper.download import CancelToken
from courts_scraper.http import Fetcher
from courts_scraper.models import JudgmentMeta, ListRow
from courts_scraper.query import Court
from courts_scraper.ratelimit import RateLimiter
from courts_scraper.run import (
    ListingPreview,
    build_run_config,
    materialize_run,
    revalidate_downloads,
    run_downloads,
    run_listing,
)

PDF_URL = "https://ww2.courts.ie/acc/alfresco/x/a.pdf/pdf"


def _fetcher(max_attempts: int = 1) -> Fetcher:
    return Fetcher(httpx.Client(), RateLimiter(0.0, 0.0), max_attempts=max_attempts)


def _config(tmp_path: Path, *, max_attempts: int = 1):
    return build_run_config(
        data_dir=tmp_path,
        courts=(Court.SUPREME,),
        delay=0.0,
        jitter=0.0,
        max_attempts=max_attempts,
        timeout=10.0,
        user_agent="test",
    )


def _listrow(*, pdf_url: str, uuid: str, judge: str = "J") -> ListRow:
    return ListRow(
        page=0,
        title="X -v- Y",
        court="Supreme Court",
        judge=judge,
        date_delivered=None,
        date_uploaded=None,
        view_url="https://ww2.courts.ie/view/x",
        pdf_url=pdf_url,
        collection_uuid="c",
        document_uuid=uuid,
    )


def _record(config, record_id: int) -> sqlite3.Row:
    conn = open_readonly(config.db_path)
    try:
        return conn.execute(
            "SELECT * FROM record WHERE id = ?", (record_id,)
        ).fetchone()
    finally:
        conn.close()


def _seed_done_run(
    data_dir: Path,
    *,
    pdf_bytes: bytes = b"%PDF-1.7 original",
    filename: str = "2026_IESC_36.pdf",
    citation: str = "[2026] IESC 36",
    pdf_url: str = PDF_URL,
    doc_uuid: str = "doc-1",
    max_attempts: int = 1,
):
    """Materialise a run with one fully-downloaded record and its PDF on disk."""
    config = _config(data_dir, max_attempts=max_attempts)
    materialize_run(config)
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    with Repository(config.db_path) as repo:
        repo.upsert_listing(
            _listrow(pdf_url=pdf_url, uuid=doc_uuid),
        )
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"], JudgmentMeta(neutral_citation=citation), filename
        )
        repo.record_download(row["id"], sha, len(pdf_bytes))
    (config.pdf_dir / filename).write_bytes(pdf_bytes)
    return config, sha


# --- Tier 1: incremental re-list -------------------------------------------
def test_relist_adds_only_new_rows_and_preserves_existing(tmp_path):
    config = _config(tmp_path)
    materialize_run(config)
    existing = _listrow(pdf_url="https://ww2.courts.ie/acc/x/1.pdf", uuid="d1")
    new = _listrow(pdf_url="https://ww2.courts.ie/acc/x/2.pdf", uuid="d2")

    with Repository(config.db_path) as repo:
        repo.upsert_listing(existing)
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"], JudgmentMeta(neutral_citation="[2026] IESC 1"), "a.pdf"
        )
        repo.record_download(row["id"], "sha", 1)
        listed_before = _record(config, row["id"])["listed_at"]

        # Re-list a single page whose rows are the existing one plus one new one.
        preview = ListingPreview(
            first_html="", first_rows=[existing, new], total_results=2, last_page=0
        )
        run_listing(config, _fetcher(), repo, preview=preview, console=Console())

        counts = repo.counts()
        assert counts["total"] == 2  # only the new row was inserted
        assert counts["download_done"] == 1  # existing row's progress preserved
        assert counts["meta_pending"] == 1  # only the new row is pending
        # The existing row's first-seen time is untouched by the re-list.
        assert _record(config, row["id"])["listed_at"] == listed_before


def test_download_fetches_only_new_rows(httpx_mock, tmp_path):
    config, _ = _seed_done_run(tmp_path)  # one done row, its PDF on disk
    new_url = "https://ww2.courts.ie/acc/x/new.pdf/pdf"
    new_bytes = b"%PDF-1.7 brand new judgment"
    with Repository(config.db_path) as repo:
        repo.upsert_listing(_listrow(pdf_url=new_url, uuid="doc-2"))
        pending = list(repo.iter_pending_metadata())
        repo.record_metadata(
            pending[0]["id"], JudgmentMeta(neutral_citation="[2026] IESC 99"), "new.pdf"
        )

        httpx_mock.add_response(url=new_url, content=new_bytes)
        run_downloads(config, _fetcher(), repo, cancel=CancelToken(), console=Console())

    # Exactly one download happened -- the done row was not re-fetched.
    requests = httpx_mock.get_requests()
    assert [str(r.url) for r in requests] == [new_url]
    assert (config.pdf_dir / "new.pdf").read_bytes() == new_bytes


# --- Tier 2: revalidation with version history -----------------------------
def test_revalidate_unchanged_skips_and_leaves_provenance(httpx_mock, tmp_path):
    original = b"%PDF-1.7 original"
    config, sha = _seed_done_run(tmp_path, pdf_bytes=original)
    before = _record(config, 1)
    httpx_mock.add_response(url=PDF_URL, content=original)

    with Repository(config.db_path) as repo:
        changed = revalidate_downloads(
            config, _fetcher(), repo, cancel=CancelToken(), console=Console()
        )
        assert changed == 0
        assert repo.count_revisions() == 0

    after = _record(config, 1)
    assert after["sha256"] == sha
    assert after["pdf_retrieved_at"] == before["pdf_retrieved_at"]  # untouched
    assert after["last_revalidated_at"] is not None  # but recorded as checked
    assert (config.pdf_dir / "2026_IESC_36.pdf").read_bytes() == original
    assert not config.versions_dir.exists()  # nothing archived


def test_revalidate_changed_versions_and_archives_old_bytes(httpx_mock, tmp_path):
    original = b"%PDF-1.7 original judgment"
    amended = b"%PDF-1.7 amended judgment text"
    config, old_sha = _seed_done_run(tmp_path, pdf_bytes=original)
    new_sha = hashlib.sha256(amended).hexdigest()
    httpx_mock.add_response(url=PDF_URL, content=amended)

    with Repository(config.db_path) as repo:
        changed = revalidate_downloads(
            config, _fetcher(), repo, cancel=CancelToken(), console=Console()
        )
        assert changed == 1
        assert repo.count_revisions() == 1

    # Live file holds the new bytes; the old bytes are archived content-addressed.
    assert (config.pdf_dir / "2026_IESC_36.pdf").read_bytes() == amended
    assert (config.versions_dir / f"{old_sha}.pdf").read_bytes() == original

    rec = _record(config, 1)
    assert rec["sha256"] == new_sha
    assert rec["bytes"] == len(amended)

    versions = read_pdf_versions(config.db_path)
    assert len(versions) == 2
    superseded = [v for v in versions if v["superseded_at"] is not None]
    current = [v for v in versions if v["superseded_at"] is None]
    assert len(superseded) == 1 and len(current) == 1
    assert superseded[0]["sha256"] == old_sha
    assert superseded[0]["filename"] == f"versions/{old_sha}.pdf"
    assert current[0]["sha256"] == new_sha
    assert current[0]["filename"] == "2026_IESC_36.pdf"


def test_revalidate_fetch_failure_leaves_row_unchecked(httpx_mock, tmp_path):
    original = b"%PDF-1.7 original"
    config, sha = _seed_done_run(tmp_path, pdf_bytes=original)
    # A single 5xx: below the outage threshold, this defers (isolated failure).
    httpx_mock.add_response(url=PDF_URL, status_code=503)

    with Repository(config.db_path) as repo:
        changed = revalidate_downloads(
            config, _fetcher(), repo, cancel=CancelToken(), console=Console()
        )
        assert changed == 0

    rec = _record(config, 1)
    assert rec["sha256"] == sha  # good file untouched
    assert rec["last_revalidated_at"] is None  # not stamped -> retried first
    assert (config.pdf_dir / "2026_IESC_36.pdf").read_bytes() == original


def test_revalidate_limit_checks_least_recently_first(httpx_mock, tmp_path):
    # Three done rows; --limit 2 must check the two never-checked ones (by id),
    # leaving the third unchecked -- proving NULLS-FIRST ordering + the cap.
    config = _config(tmp_path)
    materialize_run(config)
    bodies = {}
    with Repository(config.db_path) as repo:
        for i in range(1, 4):
            url = f"https://ww2.courts.ie/acc/x/{i}.pdf/pdf"
            body = f"%PDF-1.7 doc {i}".encode()
            bodies[i] = (url, body)
            repo.upsert_listing(_listrow(pdf_url=url, uuid=f"doc-{i}", judge=f"J{i}"))
        for i, row in enumerate(list(repo.iter_pending_metadata()), start=1):
            repo.record_metadata(
                row["id"], JudgmentMeta(neutral_citation=f"[2026] IESC {i}"), f"{i}.pdf"
            )
            repo.record_download(row["id"], hashlib.sha256(bodies[i][1]).hexdigest(), 1)
            (config.pdf_dir / f"{i}.pdf").write_bytes(bodies[i][1])

    httpx_mock.add_response(url=bodies[1][0], content=bodies[1][1])
    httpx_mock.add_response(url=bodies[2][0], content=bodies[2][1])

    with Repository(config.db_path) as repo:
        revalidate_downloads(
            config, _fetcher(), repo, cancel=CancelToken(), limit=2, console=Console()
        )

    assert _record(config, 1)["last_revalidated_at"] is not None
    assert _record(config, 2)["last_revalidated_at"] is not None
    assert _record(config, 3)["last_revalidated_at"] is None  # deferred to next run
    # Only the first two were fetched.
    assert {str(r.url) for r in httpx_mock.get_requests()} == {
        bodies[1][0],
        bodies[2][0],
    }


# --- corpus snapshot revisions summary -------------------------------------
def test_corpus_snapshot_includes_revisions_and_archived_bytes(httpx_mock, tmp_path):
    from courts_scraper.corpus import build_corpus

    original = b"%PDF-1.7 original judgment"
    amended = b"%PDF-1.7 amended judgment text"
    config, old_sha = _seed_done_run(tmp_path / "data", pdf_bytes=original)
    new_sha = hashlib.sha256(amended).hexdigest()
    httpx_mock.add_response(url=PDF_URL, content=amended)
    with Repository(config.db_path) as repo:
        revalidate_downloads(
            config, _fetcher(), repo, cancel=CancelToken(), console=Console()
        )

    out = tmp_path / "corpus"
    build_corpus([config.run_dir], out, formats=["csv", "json"])

    snapshot = json.loads((out / "data" / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["revisions"]["count"] == 1
    assert snapshot["revisions"]["truncated"] is False
    entry = snapshot["revisions"]["entries"][0]
    assert entry["old_sha256"] == old_sha
    assert entry["new_sha256"] == new_sha
    assert entry["document_uuid"] == "doc-1"
    # Superseded bytes travel inside the bag (and thus under manifest-sha256 fixity).
    archived = out / "data" / "pdfs" / "versions" / f"{old_sha}.pdf"
    assert archived.read_bytes() == original
    manifest = (out / "manifest-sha256.txt").read_text(encoding="utf-8")
    assert f"pdfs/versions/{old_sha}.pdf" in manifest


# --- confirmation / guardrails ---------------------------------------------
def test_confirm_update_revalidate_is_loud_and_gated_without_yes(tmp_path, capsys):
    config, _ = _seed_done_run(tmp_path)
    preview = ListingPreview(first_html="", first_rows=[], total_results=1, last_page=0)
    # With --yes: prints the explicit full-re-fetch cost, does not block.
    _confirm_update(config, preview, revalidate=True, yes=True)
    out = capsys.readouterr().out.lower()
    assert "revalidate" in out
    # Non-interactive without --yes: hard-blocked (never silently full-recrawls).
    with pytest.raises(typer.BadParameter):
        _confirm_update(config, preview, revalidate=True, yes=False)


# --- review-hardening: lock, self-heal, fetch cutoff -----------------------
def test_run_lock_blocks_a_second_holder(tmp_path):
    from courts_scraper.run import RunLocked, run_lock

    run = tmp_path / "run"
    run.mkdir()
    with run_lock(run), pytest.raises(RunLocked), run_lock(run):
        pass


def test_revalidate_heals_a_missing_live_file(httpx_mock, tmp_path):
    original = b"%PDF-1.7 original"
    config, sha = _seed_done_run(tmp_path, pdf_bytes=original)
    live = config.pdf_dir / "2026_IESC_36.pdf"
    live.unlink()  # simulate a lost live file; server still serves the same bytes
    httpx_mock.add_response(url=PDF_URL, content=original)

    with Repository(config.db_path) as repo:
        changed = revalidate_downloads(
            config, _fetcher(), repo, cancel=CancelToken(), console=Console()
        )
        assert changed == 0  # not a content change
        assert repo.count_revisions() == 0

    assert live.read_bytes() == original  # the file was restored, not left missing
    rec = _record(config, 1)
    assert rec["sha256"] == sha
    assert rec["last_revalidated_at"] is not None


def test_revalidate_skips_rows_fetched_after_cutoff(httpx_mock, tmp_path):
    # A combined `update --revalidate` must not re-download what it just fetched.
    original = b"%PDF-1.7 original"
    config, _ = _seed_done_run(tmp_path, pdf_bytes=original)
    cutoff = _record(config, 1)["pdf_retrieved_at"]  # the row's own fetch time

    with Repository(config.db_path) as repo:
        changed = revalidate_downloads(
            config,
            _fetcher(),
            repo,
            cancel=CancelToken(),
            fetched_before=cutoff,
            console=Console(),
        )

    assert changed == 0
    assert httpx_mock.get_requests() == []  # the just-fetched row was skipped


# --- CLI guards (no network) -----------------------------------------------
def test_update_missing_manifest_errors(tmp_path):
    from typer.testing import CliRunner

    from courts_scraper.cli import app

    norun = tmp_path / "data" / "norun"
    norun.mkdir(parents=True)
    result = CliRunner().invoke(
        app,
        ["update", "--run-dir", str(norun), "--yes"],
        env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"},
    )
    assert result.exit_code != 0
    assert "manifest" in result.output.lower()


def test_update_empty_run_is_guided(tmp_path):
    from typer.testing import CliRunner

    from courts_scraper.cli import app

    config = _config(tmp_path)
    materialize_run(config)  # manifest written, but no baseline listing
    result = CliRunner().invoke(
        app,
        ["update", "--run-dir", str(config.run_dir), "--yes"],
        env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"},
    )
    assert result.exit_code != 0
    assert "baseline" in result.output.lower()
