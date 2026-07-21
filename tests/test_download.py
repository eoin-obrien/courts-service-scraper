import hashlib

import httpx
import pytest

from courts_scraper.download import (
    CancelToken,
    DownloadCancelled,
    DownloadIncomplete,
    _content_length,
    download_pdf,
    sha256_of,
    sweep_partials,
)
from courts_scraper.http import Fetcher
from courts_scraper.ratelimit import RateLimiter

URL = "https://ww2.courts.ie/acc/alfresco/doc/judgment.pdf/pdf"


def _fetcher() -> Fetcher:
    limiter = RateLimiter(0.0, 0.0)
    return Fetcher(httpx.Client(), limiter, max_attempts=4)


class CancelAfter(CancelToken):
    """Reports 'not cancelled' for the first N checks, then 'cancelled'."""

    def __init__(self, after: int) -> None:
        super().__init__()
        self._checks = 0
        self._after = after

    @property
    def cancelled(self) -> bool:
        self._checks += 1
        return self._checks > self._after


def test_happy_path_writes_verified_file(httpx_mock, tmp_path):
    data = b"%PDF-1.7 fake body"
    httpx_mock.add_response(url=URL, content=data)
    target = tmp_path / "out.pdf"

    result = download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert target.read_bytes() == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)
    assert not (tmp_path / "out.pdf.part").exists()


def test_captures_response_provenance_headers(httpx_mock, tmp_path):
    data = b"%PDF-1.7 fake body"
    httpx_mock.add_response(
        url=URL,
        content=data,
        headers={
            "last-modified": "Wed, 02 Jul 2026 09:00:00 GMT",
            "etag": '"abc123"',
            "content-type": "application/pdf",
        },
    )
    target = tmp_path / "out.pdf"

    result = download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert result.last_modified == "Wed, 02 Jul 2026 09:00:00 GMT"
    assert result.etag == '"abc123"'
    assert result.content_type == "application/pdf"
    # httpx sets Content-Length on a fixed-body mock response.
    assert result.content_length == len(data)


def test_provenance_headers_absent_are_none(httpx_mock, tmp_path):
    # The real site usually omits these; missing headers must be None, not error.
    httpx_mock.add_response(url=URL, content=b"%PDF-1.7 body")
    target = tmp_path / "out.pdf"

    result = download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert result.last_modified is None
    assert result.etag is None


def test_empty_body_rejected(httpx_mock, tmp_path):
    httpx_mock.add_response(url=URL, content=b"")
    target = tmp_path / "out.pdf"

    with pytest.raises(DownloadIncomplete):
        download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert not target.exists()
    assert not (tmp_path / "out.pdf.part").exists()


def test_non_pdf_body_rejected(httpx_mock, tmp_path):
    # A 200 response carrying an HTML error page must not be accepted as a PDF.
    httpx_mock.add_response(url=URL, content=b"<html>error</html>")
    target = tmp_path / "out.pdf"

    with pytest.raises(DownloadIncomplete):
        download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert not target.exists()
    assert not (tmp_path / "out.pdf.part").exists()


def test_cancellation_leaves_no_file(httpx_mock, tmp_path):
    # Two chunks (>64 KiB) so cancellation lands mid-stream after one chunk.
    data = b"x" * 130_000
    httpx_mock.add_response(url=URL, content=data)
    target = tmp_path / "out.pdf"

    with pytest.raises(DownloadCancelled):
        download_pdf(_fetcher(), URL, target, cancel=CancelAfter(after=1))

    assert not target.exists()
    assert not (tmp_path / "out.pdf.part").exists()


def test_read_timeout_is_retried_then_succeeds(httpx_mock, tmp_path):
    # ReadTimeout is an httpx.TimeoutException -> retryable with backoff.
    data = b"%PDF-1.7 recovered after timeout"
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=URL)
    httpx_mock.add_response(url=URL, content=data)
    target = tmp_path / "out.pdf"

    result = download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert target.read_bytes() == data
    assert result.size == len(data)


def test_get_text_retries_read_timeout(httpx_mock):
    url = "https://ww2.courts.ie/search/Judgments/x"
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), url=url)
    httpx_mock.add_response(url=url, text="recovered")

    assert _fetcher().get_text(url) == "recovered"


def test_transient_error_is_retried_then_succeeds(httpx_mock, tmp_path):
    data = b"%PDF-1.7 recovered body"
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, content=data)
    target = tmp_path / "out.pdf"

    result = download_pdf(_fetcher(), URL, target, cancel=CancelToken())

    assert target.read_bytes() == data
    assert result.size == len(data)


def test_sweep_partials_removes_only_part_files(tmp_path):
    (tmp_path / "keep.pdf").write_bytes(b"final")
    (tmp_path / "a.pdf.part").write_bytes(b"partial")
    (tmp_path / "b.pdf.part").write_bytes(b"partial")

    removed = sweep_partials(tmp_path)

    assert removed == 2
    assert (tmp_path / "keep.pdf").exists()
    assert not list(tmp_path.glob("*.part"))


def test_is_up_true_when_server_responds(httpx_mock):
    httpx_mock.add_response(method="HEAD", url=URL, status_code=200)
    assert _fetcher().is_up(URL) is True


def test_is_up_false_on_connection_error(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("refused"), method="HEAD", url=URL)
    assert _fetcher().is_up(URL) is False


def test_sha256_of(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    assert sha256_of(path) == hashlib.sha256(b"hello").hexdigest()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"content-length": "42"}, 42),
        ({"content-length": "0"}, 0),
        ({}, None),
        ({"content-length": "-1"}, None),
        ({"content-length": "not-a-number"}, None),
    ],
)
def test_content_length(headers, expected):
    class FakeResponse:
        pass

    resp = FakeResponse()
    resp.headers = headers
    assert _content_length(resp) == expected
