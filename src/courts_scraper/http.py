"""HTTP access layer: a rate-limited, retrying client wrapper.

All outbound traffic goes through :class:`Fetcher`, which applies the politeness
gate before every request and retries transient failures (network errors,
timeouts and ``429``/``5xx`` responses) with exponential backoff. Persistent
failures raise, so callers can record an error and move on.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from courts_scraper.models import RunConfig
from courts_scraper.ratelimit import RateLimiter

# Status codes worth retrying: rate limiting and transient server faults.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Return whether an exception represents a transient, retryable failure."""
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return False


def make_client(config: RunConfig) -> httpx.Client:
    """Create an :class:`httpx.Client` configured for the run."""
    return build_client(user_agent=config.user_agent, timeout=config.timeout)


def build_client(*, user_agent: str, timeout: float) -> httpx.Client:
    """Create an :class:`httpx.Client` from explicit settings (no run folder)."""
    return httpx.Client(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=True,
    )


class Fetcher:
    """A polite, retrying facade over an :class:`httpx.Client`."""

    def __init__(
        self, client: httpx.Client, limiter: RateLimiter, max_attempts: int
    ) -> None:
        """Wrap ``client`` with a rate ``limiter`` and a retry budget."""
        self._client = client
        self._limiter = limiter
        self._retry = retry(
            retry=retry_if_exception(is_retryable),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential_jitter(initial=1.0, max=30.0),
            reraise=True,
        )

    def get_text(self, url: str) -> str:
        """Fetch ``url`` and return its body as text, retrying transient errors."""
        return self._retry(self._get_text)(url)

    def _get_text(self, url: str) -> str:
        self._limiter.wait()
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    @contextmanager
    def stream(self, url: str) -> Iterator[httpx.Response]:
        """Open a rate-limited streaming GET for ``url``.

        Retrying is intentionally *not* done here: the download layer owns the
        retry loop because it must also clean up partial files and honour
        cancellation between attempts.
        """
        self._limiter.wait()
        response = self._open_stream(url)
        try:
            yield response
        finally:
            response.close()

    def _open_stream(self, url: str) -> httpx.Response:
        response = self._client.send(
            self._client.build_request("GET", url), stream=True
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            response.close()
            raise
        return response
