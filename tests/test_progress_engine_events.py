"""Integration: the engine emits the right event sequence through a reporter."""

from __future__ import annotations

import httpx

from courts_scraper.db import Repository
from courts_scraper.download import CancelToken
from courts_scraper.models import ListRow
from courts_scraper.progress import (
    Cancelled,
    Event,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    PhaseFinished,
    PhaseStarted,
    RequestStarted,
)
from courts_scraper.query import Court, search_url
from courts_scraper.run import (
    build_fetcher,
    build_run_config,
    materialize_run,
    run_downloads,
    run_metadata,
)

_PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class RecordingReporter:
    """A reporter test double that captures every event, in order."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def __enter__(self) -> RecordingReporter:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def finished(self, status: ItemStatus) -> list[ItemFinished]:
        return [
            e for e in self.events if isinstance(e, ItemFinished) and e.status is status
        ]


def _config(tmp_path):
    return build_run_config(
        data_dir=tmp_path,
        courts=(Court.SUPREME,),
        delay=0.0,
        jitter=0.0,
        max_attempts=1,
        timeout=10.0,
        user_agent="test",
    )


def _fetcher(reporter):
    fetcher = build_fetcher(
        delay=0.0, jitter=0.0, max_attempts=1, timeout=10.0, user_agent="test"
    )
    fetcher.set_reporter(reporter)
    return fetcher


def _seed_row(repo, *, view_url, pdf_url="https://ww2.courts.ie/x/a.pdf"):
    repo.upsert_listing(
        ListRow(
            page=0,
            title="X -v- Y",
            court="Supreme Court",
            judge="J",
            date_delivered=None,
            date_uploaded=None,
            view_url=view_url,
            pdf_url=pdf_url,
            collection_uuid="c",
            document_uuid="d",
        )
    )


def test_metadata_ok_emits_request_started_then_ok(httpx_mock, view_html, tmp_path):
    config = _config(tmp_path)
    materialize_run(config)
    view_url = "https://ww2.courts.ie/view/x"
    httpx_mock.add_response(url=view_url, text=view_html)
    rec = RecordingReporter()
    with Repository(config.db_path) as repo:
        _seed_row(repo, view_url=view_url)
        run_metadata(config, _fetcher(rec), repo, cancel=CancelToken(), reporter=rec)

    kinds = [type(e).__name__ for e in rec.events]
    assert kinds[0] == "PhaseStarted"
    assert kinds[-1] == "PhaseFinished"
    # H2: the real request start is announced from the fetcher layer, before OK.
    i_started = kinds.index("ItemStarted")
    i_request = kinds.index("RequestStarted")
    i_ok = next(
        n
        for n, e in enumerate(rec.events)
        if isinstance(e, ItemFinished) and e.status is ItemStatus.OK
    )
    assert i_started < i_request < i_ok


def test_metadata_missing_citation_emits_skipped(
    httpx_mock, view_no_citation_html, tmp_path
):
    config = _config(tmp_path)
    materialize_run(config)
    view_url = "https://ww2.courts.ie/view/nocite"
    httpx_mock.add_response(url=view_url, text=view_no_citation_html)
    rec = RecordingReporter()
    with Repository(config.db_path) as repo:
        _seed_row(repo, view_url=view_url)
        run_metadata(config, _fetcher(rec), repo, cancel=CancelToken(), reporter=rec)
    assert len(rec.finished(ItemStatus.SKIPPED_NO_CITATION)) == 1
    assert rec.finished(ItemStatus.OK) == []


def test_metadata_timeout_emits_deferred(tmp_path):
    config = _config(tmp_path)
    materialize_run(config)
    rec = RecordingReporter()

    class _AlwaysTimeout:
        def get_text(self, url: str) -> str:
            raise httpx.ReadTimeout("down")

        def is_up(self, url: str) -> bool:
            return False

    with Repository(config.db_path) as repo:
        _seed_row(repo, view_url="https://ww2.courts.ie/view/x")
        run_metadata(config, _AlwaysTimeout(), repo, cancel=CancelToken(), reporter=rec)
    assert len(rec.finished(ItemStatus.DEFERRED)) == 1


def test_download_ok_emits_request_started_then_ok(httpx_mock, tmp_path):
    config = _config(tmp_path)
    materialize_run(config)
    pdf_url = "https://ww2.courts.ie/x/a.pdf"
    httpx_mock.add_response(url=pdf_url, content=_PDF_BYTES)
    rec = RecordingReporter()
    from courts_scraper.models import JudgmentMeta

    with Repository(config.db_path) as repo:
        _seed_row(repo, view_url="https://ww2.courts.ie/view/x", pdf_url=pdf_url)
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"], JudgmentMeta(neutral_citation="[2026] IESC 1"), "a.pdf"
        )
        run_downloads(config, _fetcher(rec), repo, cancel=CancelToken(), reporter=rec)
    assert len(rec.finished(ItemStatus.OK)) == 1
    assert any(isinstance(e, RequestStarted) for e in rec.events)


def test_download_tampered_filename_emits_error(tmp_path):
    config = _config(tmp_path)
    materialize_run(config)
    rec = RecordingReporter()
    from courts_scraper.models import JudgmentMeta

    with Repository(config.db_path) as repo:
        _seed_row(repo, view_url="https://ww2.courts.ie/view/x")
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"], JudgmentMeta(neutral_citation="[2026] IESC 1"), "../../evil.pdf"
        )
        run_downloads(config, _fetcher(rec), repo, cancel=CancelToken(), reporter=rec)
    assert len(rec.finished(ItemStatus.ERROR)) == 1


def test_cancel_emits_cancelled(tmp_path):
    config = _config(tmp_path)
    materialize_run(config)
    rec = RecordingReporter()
    cancel = CancelToken()
    cancel.cancel()  # pre-cancelled: the loop should stop at the first item
    from courts_scraper.models import JudgmentMeta

    with Repository(config.db_path) as repo:
        _seed_row(repo, view_url="https://ww2.courts.ie/view/x")
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"], JudgmentMeta(neutral_citation="[2026] IESC 1"), "a.pdf"
        )
        run_downloads(config, _fetcher(rec), repo, cancel=cancel, reporter=rec)
    assert any(isinstance(e, Cancelled) for e in rec.events)


def test_listing_emits_phase_and_page_items(httpx_mock, search_html, tmp_path):
    from courts_scraper.run import preview_listing, run_listing

    config = _config(tmp_path)
    materialize_run(config)
    httpx_mock.add_response(
        url=search_url(config.base_url, config.query, page=0), text=search_html
    )
    rec = RecordingReporter()
    fetcher = build_fetcher(
        delay=0.0, jitter=0.0, max_attempts=1, timeout=10.0, user_agent="test"
    )
    # Preview happens before the reporter is attached (as in the CLI), so the
    # preview's page-0 request is not recorded here.
    preview = preview_listing(config, fetcher, max_pages=1)
    fetcher.set_reporter(rec)
    with Repository(config.db_path) as repo:
        run_listing(config, fetcher, repo, preview=preview, reporter=rec)
    assert isinstance(rec.events[0], PhaseStarted)
    assert rec.events[0].phase == "listing"
    assert any(isinstance(e, ItemStarted) for e in rec.events)
    assert isinstance(rec.events[-1], PhaseFinished)
