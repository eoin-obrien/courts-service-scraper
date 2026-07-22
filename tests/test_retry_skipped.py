"""Tests for --retry-skipped: re-queueing previously-skipped metadata rows."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from courts_scraper.cli import app
from courts_scraper.db import Repository
from courts_scraper.download import CancelToken
from courts_scraper.models import JudgmentMeta, ListRow
from courts_scraper.query import Court
from courts_scraper.run import (
    build_fetcher,
    build_run_config,
    materialize_run,
    run_metadata,
)

runner = CliRunner()
_WIDE_ENV = {"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI.sub("", text)


def _row(view_url="https://ww2.courts.ie/view/x", judge="J", document_uuid="d"):
    return ListRow(
        page=0,
        title="X -v- Y",
        court="Supreme Court",
        judge=judge,
        date_delivered=None,
        date_uploaded=None,
        view_url=view_url,
        # pdf_url is UNIQUE (the upsert conflict key), so make it per-document.
        pdf_url=f"https://ww2.courts.ie/x/{document_uuid}.pdf",
        collection_uuid="c",
        document_uuid=document_uuid,
    )


def test_reset_meta_errors_requeues_only_errors(tmp_path):
    with Repository(tmp_path / "db.sqlite") as repo:
        for i in range(4):
            repo.upsert_listing(_row(judge=f"J{i}", document_uuid=f"d{i}"))
        rows = list(repo.iter_pending_metadata())
        # rows[0] -> ok; rows[1], rows[2] -> error; rows[3] -> stays pending.
        repo.record_metadata(rows[0]["id"], JudgmentMeta("[2026] IESC 1"), "a.pdf")
        repo.record_meta_error(rows[1]["id"], "no neutral citation present")
        repo.record_meta_error(rows[2]["id"], "ValueError: bad page")

        assert repo.counts()["meta_error"] == 2
        # Errors are terminal for the normal metadata phase.
        assert len(list(repo.iter_pending_metadata())) == 1

        requeued = repo.reset_meta_errors()

        assert requeued == 2
        counts = repo.counts()
        assert counts["meta_error"] == 0
        assert counts["meta_ok"] == 1  # the resolved one is untouched
        assert counts["meta_pending"] == 3  # 1 original + 2 re-queued
        # error_reason is cleared on the re-queued rows.
        assert len(list(repo.iter_pending_metadata())) == 3


def test_retry_resolves_a_previously_skipped_row(httpx_mock, view_html, tmp_path):
    """A row skipped for 'no citation' resolves once re-queued and re-scraped."""
    config = build_run_config(
        data_dir=tmp_path,
        courts=(Court.SUPREME,),
        delay=0.0,
        jitter=0.0,
        max_attempts=1,
        timeout=10.0,
        user_agent="test",
    )
    materialize_run(config)
    view_url = "https://ww2.courts.ie/view/x"
    # The view page now carries a citation (the site has backfilled it).
    httpx_mock.add_response(url=view_url, text=view_html)

    with Repository(config.db_path) as repo:
        repo.upsert_listing(_row(view_url=view_url))
        (row,) = list(repo.iter_pending_metadata())
        # Simulate an earlier pass that skipped it for a missing citation.
        repo.record_meta_error(row["id"], "no neutral citation present")
        assert repo.counts()["meta_error"] == 1

        repo.reset_meta_errors()
        fetcher = build_fetcher(
            delay=0.0, jitter=0.0, max_attempts=1, timeout=10.0, user_agent="test"
        )
        run_metadata(config, fetcher, repo, cancel=CancelToken())
        counts = repo.counts()

    assert counts["meta_ok"] == 1
    assert counts["meta_error"] == 0


def test_fetch_court_and_retry_skipped_conflict():
    result = runner.invoke(
        app, ["fetch", "--court", "supreme", "--retry-skipped"], env=_WIDE_ENV
    )
    assert result.exit_code == 2
    assert "retry-skipped" in _clean(result.output)


def test_retry_skipped_listed_on_fetch_and_update_help():
    for command in ("fetch", "update"):
        result = runner.invoke(app, [command, "--help"], env=_WIDE_ENV)
        assert result.exit_code == 0
        assert "--retry-skipped" in _clean(result.output)
