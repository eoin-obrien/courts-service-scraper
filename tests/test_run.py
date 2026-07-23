import json

import pytest

from courts_scraper.query import Court, search_url
from courts_scraper.run import (
    build_fetcher,
    build_run_config,
    estimate_seconds,
    format_duration,
    preview_listing,
)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (45, "45s"),
        (600, "10m"),
        (3600, "1h 0m"),
        (3900, "1h 5m"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_estimate_seconds_uses_delay_plus_half_jitter():
    assert estimate_seconds(10, delay=5.0, jitter=2.0) == 60.0


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


def _fetcher():
    return build_fetcher(
        delay=0.0, jitter=0.0, max_attempts=1, timeout=10.0, user_agent="test"
    )


def test_preview_listing_reports_scale(httpx_mock, search_html, tmp_path):
    config = _config(tmp_path)
    httpx_mock.add_response(
        url=search_url(config.base_url, config.query, page=0), text=search_html
    )

    preview = preview_listing(config, _fetcher(), max_pages=None)

    assert preview.total_results == 2561
    assert preview.last_page == 25
    assert preview.total_pages == 26
    assert len(preview.first_rows) == 100
    # No cap => the full crawl, not truncated.
    assert preview.pages_available == 26
    assert preview.truncated is False


def test_preview_listing_respects_max_pages(httpx_mock, search_html, tmp_path):
    config = _config(tmp_path)
    httpx_mock.add_response(
        url=search_url(config.base_url, config.query, page=0), text=search_html
    )

    preview = preview_listing(config, _fetcher(), max_pages=1)

    assert preview.last_page == 0
    assert preview.total_pages == 1
    # The uncapped size is preserved so truncation is detectable.
    assert preview.pages_available == 26
    assert preview.max_pages == 1
    assert preview.truncated is True


def test_preview_listing_max_pages_at_or_above_real_is_not_truncated(
    httpx_mock, search_html, tmp_path
):
    config = _config(tmp_path)
    httpx_mock.add_response(
        url=search_url(config.base_url, config.query, page=0), text=search_html
    )

    # 26 real pages; capping at exactly 26 (boundary) does not cut anything.
    preview = preview_listing(config, _fetcher(), max_pages=26)

    assert preview.total_pages == 26
    assert preview.pages_available == 26
    assert preview.truncated is False


def test_finalize_listing_records_truncation(tmp_path):
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)  # writes the creation-time manifest

    preview = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=2,
        max_pages=3,
        pages_available=26,
    )
    finalize_listing(config, preview)

    block = json.loads(config.manifest_path.read_text(encoding="utf-8"))["listing"]
    assert block == {
        "complete": True,
        "truncated": True,
        "max_pages": 3,
        "pages_fetched": 3,
        "pages_available": 26,
    }


def test_finalize_listing_full_crawl_is_not_truncated(tmp_path):
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)

    preview = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=25,
        max_pages=None,
        pages_available=26,
    )
    finalize_listing(config, preview)

    block = json.loads(config.manifest_path.read_text(encoding="utf-8"))["listing"]
    assert block["complete"] is True
    assert block["truncated"] is False


def test_finalize_listing_clear_only_keeps_prior_full_crawl(tmp_path):
    """A later capped pass must not un-cover pages an earlier full crawl reached."""
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)

    full = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=25,
        max_pages=None,
        pages_available=26,
    )
    finalize_listing(config, full)  # verified full crawl

    capped = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=2,
        max_pages=3,
        pages_available=26,
    )
    finalize_listing(config, capped)  # a narrow update pass

    block = json.loads(config.manifest_path.read_text(encoding="utf-8"))["listing"]
    # Coverage is monotonic: the full crawl's verdict survives the capped pass.
    assert block["truncated"] is False
    assert block["pages_fetched"] == 26


def test_finalize_listing_full_pass_clears_prior_truncation(tmp_path):
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)

    capped = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=2,
        max_pages=3,
        pages_available=26,
    )
    finalize_listing(config, capped)  # truncated first

    full = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=25,
        max_pages=None,
        pages_available=26,
    )
    finalize_listing(config, full)  # then backfilled by a full pass

    block = json.loads(config.manifest_path.read_text(encoding="utf-8"))["listing"]
    assert block["truncated"] is False


def test_finalize_listing_grow_then_cap_is_truncated(tmp_path):
    """The result set grows between runs; a capped update that no longer reaches
    the new end must flip a prior full crawl back to truncated (not trust it)."""
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)

    full = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=26,
        max_pages=None,
        pages_available=27,
    )
    finalize_listing(config, full)  # verified full crawl of 27 pages

    # Site grew to 40 pages; a capped update re-lists only 30.
    grown_capped = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=4000,
        last_page=29,
        max_pages=30,
        pages_available=40,
    )
    finalize_listing(config, grown_capped)

    block = json.loads(config.manifest_path.read_text(encoding="utf-8"))["listing"]
    # Covers max(30, 27) = 30 of 40 -> genuinely truncated now.
    assert block["truncated"] is True
    assert block["pages_fetched"] == 30
    assert block["pages_available"] == 40


def test_finalize_listing_smaller_cap_does_not_lower_recorded_coverage(tmp_path):
    """A narrower capped pass over a wider prior one keeps the larger coverage
    (listing only upserts, so the union is the largest prefix ever fetched)."""
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)

    wider = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=9,
        max_pages=10,
        pages_available=27,
    )
    finalize_listing(config, wider)  # truncated at 10/27

    narrower = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=4,
        max_pages=5,
        pages_available=27,
    )
    finalize_listing(config, narrower)  # a narrower 5-page pass

    block = json.loads(config.manifest_path.read_text(encoding="utf-8"))["listing"]
    assert block["truncated"] is True
    assert block["pages_fetched"] == 10  # not lowered to 5


def test_finalize_listing_writes_atomically_no_temp_left(tmp_path):
    from courts_scraper.run import ListingPreview, finalize_listing, materialize_run

    config = _config(tmp_path)
    materialize_run(config)
    preview = ListingPreview(
        first_html="",
        first_rows=[],
        total_results=2561,
        last_page=2,
        max_pages=3,
        pages_available=26,
    )
    finalize_listing(config, preview)

    # The manifest is valid JSON and no .tmp scratch file is left behind.
    json.loads(config.manifest_path.read_text(encoding="utf-8"))
    leftovers = list(config.run_dir.glob("manifest.json.*.tmp"))
    assert leftovers == []


def test_download_rejects_tampered_filename(tmp_path):
    """A malicious filename in the DB is refused at the write boundary."""
    from courts_scraper.db import Repository
    from courts_scraper.download import CancelToken
    from courts_scraper.models import JudgmentMeta, ListRow
    from courts_scraper.run import materialize_run, run_downloads

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

    with Repository(config.db_path) as repo:
        repo.upsert_listing(
            ListRow(
                page=0,
                title="X -v- Y",
                court="Supreme Court",
                judge="J",
                date_delivered=None,
                date_uploaded=None,
                view_url="https://ww2.courts.ie/view/x",
                pdf_url="https://ww2.courts.ie/acc/alfresco/x/a.pdf",
                collection_uuid="c",
                document_uuid="d",
            )
        )
        (row,) = list(repo.iter_pending_metadata())
        # Simulate a tampered row: a filename that escapes the PDF folder.
        repo.record_metadata(
            row["id"],
            JudgmentMeta(neutral_citation="[2026] IESC 36"),
            "../../evil.pdf",
        )

        run_downloads(config, _fetcher(), repo, cancel=CancelToken())
        counts = repo.counts()

    assert counts["download_error"] == 1
    assert counts["download_done"] == 0
    # The escape target was never created (the download never ran).
    assert not (tmp_path / "evil.pdf").exists()
    assert not (config.pdf_dir / "evil.pdf").exists()


def test_metadata_fetch_failure_does_not_crash_run(tmp_path):
    """A ReadTimeout that outlives retries leaves the row pending, not a crash."""
    import httpx

    from courts_scraper.db import Repository
    from courts_scraper.download import CancelToken
    from courts_scraper.models import ListRow
    from courts_scraper.run import materialize_run, run_metadata

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

    class _AlwaysTimeout:
        def get_text(self, url: str) -> str:
            raise httpx.ReadTimeout("read timed out")

        def is_up(self, url: str) -> bool:
            return False

    with Repository(config.db_path) as repo:
        repo.upsert_listing(
            ListRow(
                page=0,
                title="X -v- Y",
                court="Supreme Court",
                judge="J",
                date_delivered=None,
                date_uploaded=None,
                view_url="https://ww2.courts.ie/view/x",
                pdf_url="https://ww2.courts.ie/acc/alfresco/x/a.pdf",
                collection_uuid="c",
                document_uuid="d",
            )
        )
        # Must not raise, even though every fetch times out.
        run_metadata(config, _AlwaysTimeout(), repo, cancel=CancelToken())
        counts = repo.counts()

    # The row stays pending (retryable on resume) -- not done, not errored.
    assert counts["meta_pending"] == 1
    assert counts["meta_ok"] == 0
    assert counts["meta_error"] == 0
    # The failure is recorded durably for follow-up.
    assert "metadata_fetch_failed" in config.error_log_path.read_text(encoding="utf-8")


def test_downloads_pause_on_a_200_maintenance_page_outage(
    tmp_path, monkeypatch, httpx_mock
):
    """Regression: a nightly outage that serves HTTP 200 + a non-PDF holding
    page must trip the outage pause, not race through failing every file.

    The server never raises an httpx error here -- every request "succeeds" with
    a 200 whose body is not a PDF -- so this exercises the path a bare
    httpx.HTTPError outage set silently misses.
    """
    import httpx

    from courts_scraper.db import Repository
    from courts_scraper.download import CancelToken
    from courts_scraper.http import Fetcher
    from courts_scraper.models import JudgmentMeta, ListRow
    from courts_scraper.progress import OutagePaused, ProgressReporter
    from courts_scraper.ratelimit import RateLimiter
    from courts_scraper.run import OutageBreaker, materialize_run, run_downloads

    # Neutralise real waiting and let the site "recover" on the first probe, so
    # the test asserts *whether* it pauses without sleeping for real.
    real_breaker = OutageBreaker

    def _fast_breaker(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _s: None)
        return real_breaker(*args, **kwargs)

    monkeypatch.setattr("courts_scraper.run.OutageBreaker", _fast_breaker)
    monkeypatch.setattr(Fetcher, "is_up", lambda self, url: True)

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

    # Five download-ready rows; the site answers every PDF request with a 200
    # maintenance page (reusable across all of them).
    httpx_mock.add_response(
        status_code=200,
        html="<html><body>Down for scheduled maintenance</body></html>",
        is_reusable=True,
    )

    class _Rec(ProgressReporter):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    with Repository(config.db_path) as repo:
        for i in range(5):
            repo.upsert_listing(
                ListRow(
                    page=0,
                    title="X -v- Y",
                    court="Supreme Court",
                    judge=f"J{i}",
                    date_delivered=None,
                    date_uploaded=None,
                    view_url=f"https://ww2.courts.ie/view/{i}",
                    pdf_url=f"https://ww2.courts.ie/acc/alfresco/x/{i}.pdf",
                    collection_uuid="c",
                    document_uuid=f"d{i}",
                )
            )
        for row in repo.iter_pending_metadata():
            repo.record_metadata(
                row["id"],
                JudgmentMeta(neutral_citation="[2026] IESC 36"),
                f"f{row['id']}.pdf",
            )

        reporter = _Rec()
        fetcher = Fetcher(httpx.Client(), RateLimiter(0.0, 0.0), max_attempts=1)
        fetcher.set_reporter(reporter)
        run_downloads(config, fetcher, repo, cancel=CancelToken(), reporter=reporter)

    paused = sum(isinstance(e, OutagePaused) for e in reporter.events)
    assert paused >= 1, "a run of 200 maintenance-page responses must trip the pause"


def test_metadata_pauses_on_a_200_outage_shell(tmp_path, monkeypatch, httpx_mock):
    """Regression: during an outage the site answers every view request with a
    200 *Judgments* shell (no metadata grid). That must trip the outage pause and
    leave rows pending -- not terminally error each one as "no neutral citation".
    """
    import httpx

    from courts_scraper.db import Repository
    from courts_scraper.download import CancelToken
    from courts_scraper.http import Fetcher
    from courts_scraper.models import ListRow
    from courts_scraper.progress import OutagePaused, ProgressReporter
    from courts_scraper.ratelimit import RateLimiter
    from courts_scraper.run import OutageBreaker, materialize_run, run_metadata

    real_breaker = OutageBreaker

    def _fast_breaker(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _s: None)
        return real_breaker(*args, **kwargs)

    monkeypatch.setattr("courts_scraper.run.OutageBreaker", _fast_breaker)
    monkeypatch.setattr(Fetcher, "is_up", lambda self, url: True)

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

    # The outage shell: a valid 200 HTML page with no span.cell-title cells.
    httpx_mock.add_response(
        status_code=200,
        html="<html><head><title>Judgments | The Courts Service of Ireland"
        "</title></head><body><div class='view-content'></div></body></html>",
        is_reusable=True,
    )

    class _Rec(ProgressReporter):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    with Repository(config.db_path) as repo:
        for i in range(5):
            repo.upsert_listing(
                ListRow(
                    page=0,
                    title="X -v- Y",
                    court="Supreme Court",
                    judge=f"J{i}",
                    date_delivered=None,
                    date_uploaded=None,
                    view_url=f"https://ww2.courts.ie/view/{i}",
                    pdf_url=f"https://ww2.courts.ie/acc/alfresco/x/{i}.pdf",
                    collection_uuid="c",
                    document_uuid=f"d{i}",
                )
            )
        reporter = _Rec()
        fetcher = Fetcher(httpx.Client(), RateLimiter(0.0, 0.0), max_attempts=1)
        fetcher.set_reporter(reporter)
        run_metadata(config, fetcher, repo, cancel=CancelToken(), reporter=reporter)
        counts = repo.counts()

    paused = sum(isinstance(e, OutagePaused) for e in reporter.events)
    assert paused >= 1, "a run of 200 outage shells must trip the pause"
    # Rows are left retryable, NOT burned as terminal 'no citation' errors.
    assert counts["meta_error"] == 0
    assert counts["meta_ok"] == 0


def test_metadata_citation_less_page_still_skips_without_pausing(
    tmp_path, monkeypatch, httpx_mock, view_no_citation_html
):
    """Guard against a false alarm: a *real* judgment page that simply lacks a
    Neutral Citation still carries its other cells, so it must keep the normal
    no-citation skip path and never trip the outage pause.
    """
    import httpx

    from courts_scraper.db import Repository
    from courts_scraper.download import CancelToken
    from courts_scraper.http import Fetcher
    from courts_scraper.models import ListRow
    from courts_scraper.progress import OutagePaused, ProgressReporter
    from courts_scraper.ratelimit import RateLimiter
    from courts_scraper.run import OutageBreaker, materialize_run, run_metadata

    real_breaker = OutageBreaker

    def _fast_breaker(*args, **kwargs):
        kwargs.setdefault("sleep", lambda _s: None)
        return real_breaker(*args, **kwargs)

    monkeypatch.setattr("courts_scraper.run.OutageBreaker", _fast_breaker)
    monkeypatch.setattr(Fetcher, "is_up", lambda self, url: True)

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
    httpx_mock.add_response(
        status_code=200, html=view_no_citation_html, is_reusable=True
    )

    class _Rec(ProgressReporter):
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    with Repository(config.db_path) as repo:
        for i in range(5):
            repo.upsert_listing(
                ListRow(
                    page=0,
                    title="X -v- Y",
                    court="Supreme Court",
                    judge=f"J{i}",
                    date_delivered=None,
                    date_uploaded=None,
                    view_url=f"https://ww2.courts.ie/view/{i}",
                    pdf_url=f"https://ww2.courts.ie/acc/alfresco/x/{i}.pdf",
                    collection_uuid="c",
                    document_uuid=f"d{i}",
                )
            )
        reporter = _Rec()
        fetcher = Fetcher(httpx.Client(), RateLimiter(0.0, 0.0), max_attempts=1)
        fetcher.set_reporter(reporter)
        run_metadata(config, fetcher, repo, cancel=CancelToken(), reporter=reporter)
        counts = repo.counts()

    paused = sum(isinstance(e, OutagePaused) for e in reporter.events)
    assert paused == 0, "a real citation-less page must not be mistaken for an outage"
    assert counts["meta_error"] == 5  # all recorded as the normal no-citation skip


def test_build_run_config_does_not_touch_disk(tmp_path):
    config = build_run_config(
        data_dir=tmp_path,
        courts=(Court.SUPREME,),
        delay=5.0,
        jitter=2.0,
        max_attempts=4,
        timeout=60.0,
        user_agent="test",
    )
    # The run folder must not exist until it is explicitly materialized.
    assert not config.run_dir.exists()


def test_manifest_records_custom_user_agent(tmp_path):
    from courts_scraper.run import materialize_run

    config = build_run_config(
        data_dir=tmp_path,
        courts=(Court.SUPREME,),
        delay=5.0,
        jitter=2.0,
        max_attempts=4,
        timeout=60.0,
        user_agent="my-custom-agent/9.9",
    )
    materialize_run(config)

    manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    assert manifest["user_agent"] == "my-custom-agent/9.9"
