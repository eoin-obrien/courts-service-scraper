"""HTTP access layer: a rate-limited, retrying client wrapper.

All outbound traffic goes through :class:`Fetcher`, which applies the politeness
gate before every request and retries transient failures (network errors,
timeouts and ``429``/``5xx`` responses) with exponential backoff. Persistent
failures raise, so callers can record an error and move on.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from courts_scraper.models import RunConfig
from courts_scraper.progress import (
    ProgressReporter,
    QuietReporter,
    RequestStarted,
    RetryScheduled,
    WaitReason,
    WaitStarted,
)
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


def retry_before_sleep(
    get_reporter: Callable[[], ProgressReporter],
    get_url: Callable[[RetryCallState], str],
    max_attempts: int,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[RetryCallState], None]:
    """Build a tenacity ``before_sleep`` callback that emits backoff events.

    Shared by :class:`Fetcher` (listing/metadata GETs) and the download layer's
    own retry loops so retry backoff -- another form of deliberate waiting -- is
    visible on every network path, including the long PDF downloads. ``get_url``
    extracts the request URL from the retry state (from the call args for the
    single-arg GET, or a bound closure for the download loop).
    """

    def before_sleep(state: RetryCallState) -> None:
        reporter = get_reporter()
        sleep_s = state.next_action.sleep if state.next_action else 0.0
        reporter.emit(
            RetryScheduled(get_url(state), state.attempt_number, max_attempts, sleep_s)
        )
        reporter.emit(
            WaitStarted(WaitReason.RETRY_BACKOFF, sleep_s, monotonic() + sleep_s)
        )

    return before_sleep


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
        self._reporter: ProgressReporter = QuietReporter()
        self._retry = retry(
            retry=retry_if_exception(is_retryable),
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential_jitter(initial=1.0, max=30.0),
            reraise=True,
            before_sleep=retry_before_sleep(
                lambda: self._reporter,
                lambda state: str(state.args[0]) if state.args else "",
                max_attempts,
            ),
        )

    @property
    def reporter(self) -> ProgressReporter:
        """The attached progress reporter (defaults to the null sink)."""
        return self._reporter

    def set_reporter(self, reporter: ProgressReporter) -> None:
        """Attach ``reporter`` here and on the underlying rate limiter.

        Settable after construction because a fetcher is built for the pre-scrape
        preview before any reporter (or dashboard) exists.
        """
        self._reporter = reporter
        self._limiter.set_reporter(reporter)

    def get_text(self, url: str) -> str:
        """Fetch ``url`` and return its body as text, retrying transient errors."""
        return self._retry(self._get_text)(url)

    def is_up(self, url: str) -> bool:
        """Single, non-retrying liveness probe used to detect site recovery.

        Returns ``True`` if the server responds at all (any status below 500),
        ``False`` on a connection error, timeout, or 5xx. The probe's spacing wait
        is not announced as politeness (the outage breaker owns that narration).
        """
        self._limiter.wait(announce=False)
        try:
            response = self._client.head(url)
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    def _get_text(self, url: str) -> str:
        self._limiter.wait()
        # The politeness wait is over; the real request starts now. Announcing it
        # here (the HTTP layer) is what clears the dashboard countdown at the exact
        # moment the request begins, instead of leaving it frozen at 0.0s.
        self._reporter.emit(RequestStarted(url))
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
        self._reporter.emit(RequestStarted(url))
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
