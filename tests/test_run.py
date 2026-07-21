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


def test_preview_listing_respects_max_pages(httpx_mock, search_html, tmp_path):
    config = _config(tmp_path)
    httpx_mock.add_response(
        url=search_url(config.base_url, config.query, page=0), text=search_html
    )

    preview = preview_listing(config, _fetcher(), max_pages=1)

    assert preview.last_page == 0
    assert preview.total_pages == 1


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
