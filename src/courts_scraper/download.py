"""Atomic, cancel-safe, checksum-verified PDF downloads.

The correctness requirement that shapes this module: **cancelling a download
must never leave a half-file that a later run mistakes for a complete one.**

The technique is write-to-temp-then-atomically-rename:

1. Stream the body into ``<name>.pdf.part``, hashing as we go.
2. Verify the byte count against ``Content-Length`` when the server sends it.
3. ``fsync`` the temp file, then ``os.replace`` it onto the final name -- an
   atomic operation on POSIX and Windows.

A file only ever appears under its final name once it is complete and verified.
On cancellation we delete the ``.part`` file and leave the database row
``pending``, so the item is simply retried next time. A startup sweep removes
any ``.part`` files orphaned by a hard kill.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from courts_scraper.http import Fetcher, is_retryable

_CHUNK = 64 * 1024
_PART_SUFFIX = ".part"

# Every judgment file served by the site is a PDF; the magic header lets us
# reject truncated bodies, empty responses and HTML error pages that arrive
# with a 200 status. The server does not send Content-Length for these
# downloads, so this is the primary integrity check.
_PDF_MAGIC = b"%PDF-"


class DownloadCancelled(Exception):
    """Raised when a download is interrupted by a cancellation request."""


class DownloadIncomplete(Exception):
    """Raised when a download's byte count disagrees with ``Content-Length``."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Outcome of a successful download."""

    sha256: str
    size: int


class CancelToken:
    """A tiny cooperative-cancellation flag, checked between chunks."""

    def __init__(self) -> None:
        """Create an un-cancelled token."""
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled


def sweep_partials(directory: Path) -> int:
    """Delete orphaned ``*.part`` files left by a previous hard interruption.

    Args:
        directory: The PDF output directory.

    Returns:
        The number of partial files removed.
    """
    if not directory.exists():
        return 0
    removed = 0
    for part in directory.glob(f"*{_PART_SUFFIX}"):
        part.unlink(missing_ok=True)
        removed += 1
    return removed


def sha256_of(path: Path) -> str:
    """Return the hex SHA-256 digest of a file's contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_pdf(
    fetcher: Fetcher,
    url: str,
    target: Path,
    *,
    cancel: CancelToken,
    max_attempts: int = 4,
) -> DownloadResult:
    """Download ``url`` to ``target`` atomically, retrying transient failures.

    Args:
        fetcher: The rate-limited HTTP facade.
        url: The PDF URL.
        target: Final destination path (e.g. ``.../2026_IESC_36_Woulfe-J.pdf``).
        cancel: Cancellation token, polled between chunks.
        max_attempts: Attempts before a transient failure is re-raised.

    Returns:
        The verified :class:`DownloadResult` (checksum and size).

    Raises:
        DownloadCancelled: If cancellation was requested mid-download.
        DownloadIncomplete: If the body was empty, was not a PDF, or disagreed
            with a comparable ``Content-Length``.
        httpx.HTTPError: If the download failed after exhausting retries.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    @retry(
        retry=retry_if_exception(is_retryable),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1.0, max=30.0),
        reraise=True,
    )
    def _attempt() -> DownloadResult:
        return _download_once(fetcher, url, target, cancel)

    return _attempt()


def _download_once(
    fetcher: Fetcher, url: str, target: Path, cancel: CancelToken
) -> DownloadResult:
    """Perform a single download attempt (may be retried by the caller)."""
    part = target.with_name(target.name + _PART_SUFFIX)
    digest = hashlib.sha256()
    size = 0
    header = b""

    try:
        with fetcher.stream(url) as response, part.open("wb") as handle:
            expected = _comparable_content_length(response)
            for chunk in response.iter_bytes(_CHUNK):
                if cancel.cancelled:
                    raise DownloadCancelled(url)
                if len(header) < len(_PDF_MAGIC):
                    header += chunk[: len(_PDF_MAGIC) - len(header)]
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)

            _verify_body(url, size=size, header=header, expected=expected)

            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Any failure -- transient error, cancellation, incomplete body --
        # leaves no ``.part`` behind and therefore no final file.
        part.unlink(missing_ok=True)
        raise

    # Atomic publish: the final name appears only now, fully written.
    # ``Path.replace`` performs an atomic ``os.replace`` under the hood; we then
    # fsync the directory so the rename itself is durable, not just the bytes.
    part.replace(target)
    _fsync_dir(target.parent)
    return DownloadResult(sha256=digest.hexdigest(), size=size)


def _verify_body(url: str, *, size: int, header: bytes, expected: int | None) -> None:
    """Validate a downloaded body before it is published under its final name.

    Raises:
        DownloadIncomplete: If the body is empty, is not a PDF, or (when the
            server sent a comparable ``Content-Length``) is the wrong length.
    """
    if size == 0:
        raise DownloadIncomplete(f"{url}: empty response body")
    if not header.startswith(_PDF_MAGIC):
        raise DownloadIncomplete(f"{url}: response is not a PDF (missing %PDF- header)")
    if expected is not None and size != expected:
        raise DownloadIncomplete(f"{url}: expected {expected} bytes, received {size}")


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory so a rename into it is durable.

    Directory fsync is unsupported on some platforms (e.g. Windows); failures
    are ignored -- durability is a safety margin, not a correctness requirement.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _comparable_content_length(response: object) -> int | None:
    """Return ``Content-Length`` only when it can be compared to bytes read.

    ``httpx`` transparently decompresses the body, so when the response carries
    a ``Content-Encoding`` the header (compressed length) will not match the
    decoded byte count we accumulate. In that case we return ``None`` and skip
    the length check rather than reporting a false "incomplete download".
    """
    headers = getattr(response, "headers", {})
    if headers.get("content-encoding"):
        return None
    return _content_length(response)


def _content_length(response: object) -> int | None:
    """Extract a non-negative integer ``Content-Length`` header, if present."""
    headers = getattr(response, "headers", {})
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
