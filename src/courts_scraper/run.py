"""Run orchestration: wires parsing, persistence and downloading together.

A *run* owns a self-contained folder (see :class:`~courts_scraper.models.RunConfig`)
holding its database, PDFs and logs, named by start time and search terms. The
two phases are:

* :func:`run_listing`   -- populate the database from the paginated search.
* :func:`run_metadata` + :func:`run_downloads` -- enrich each row from its view
  page and download its PDF, resumably and politely.

Everything here is resume-safe: rerunning a phase only does outstanding work.
"""

from __future__ import annotations

import json
import signal
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from courts_scraper import __version__
from courts_scraper.db import Repository
from courts_scraper.download import (
    CancelToken,
    DownloadCancelled,
    download_pdf,
    sweep_partials,
)
from courts_scraper.http import Fetcher, make_client
from courts_scraper.models import ListRow, RunConfig
from courts_scraper.naming import MissingCitationError, pdf_filename
from courts_scraper.parse_list import (
    parse_last_page,
    parse_result_count,
    parse_search_page,
)
from courts_scraper.parse_view import parse_view_page
from courts_scraper.query import Court, build_query
from courts_scraper.ratelimit import RateLimiter

DEFAULT_BASE_URL = "https://ww2.courts.ie"


class ListingError(RuntimeError):
    """Raised when the search results page cannot be parsed as expected.

    Signals likely HTML-structure drift, so the run stops instead of recording
    a silently empty result set.
    """


# ---------------------------------------------------------------------------
# Run configuration and folders
# ---------------------------------------------------------------------------
def _slugify_courts(courts: tuple[Court, ...]) -> str:
    return "-".join(c.name.lower() for c in courts)


def new_run_config(
    *,
    data_dir: Path,
    courts: tuple[Court, ...],
    base_url: str = DEFAULT_BASE_URL,
    delay: float,
    jitter: float,
    max_attempts: int,
    timeout: float,
    user_agent: str,
) -> RunConfig:
    """Create a fresh run folder and its :class:`RunConfig`, writing a manifest.

    The folder name is ``<UTC timestamp>__<court-slug>`` so runs sort
    chronologically and are self-describing.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = data_dir / f"{stamp}__{_slugify_courts(courts)}"
    config = RunConfig(
        run_dir=run_dir,
        base_url=base_url,
        query=build_query(courts),
        courts=tuple(c.value for c in courts),
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=user_agent,
    )
    _prepare_dirs(config)
    _write_manifest(config)
    return config


def load_run_config(
    run_dir: Path,
    *,
    delay: float,
    jitter: float,
    max_attempts: int,
    timeout: float,
    user_agent: str,
) -> RunConfig:
    """Rebuild a :class:`RunConfig` from an existing run's manifest (for resume).

    Runtime parameters (politeness, timeout, user agent) are taken from the
    current invocation; the search identity comes from the stored manifest.

    Raises:
        FileNotFoundError: If ``run_dir`` has no ``manifest.json``.
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest.json in {run_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = RunConfig(
        run_dir=run_dir,
        base_url=manifest["base_url"],
        query=manifest["query"],
        courts=tuple(manifest["courts"]),
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=user_agent,
    )
    _prepare_dirs(config)
    return config


def _prepare_dirs(config: RunConfig) -> None:
    config.run_dir.mkdir(parents=True, exist_ok=True)
    config.pdf_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)


def _write_manifest(config: RunConfig) -> None:
    manifest = {
        "tool": "courts-scraper",
        "version": __version__,
        "created": datetime.now(UTC).isoformat(),
        "base_url": config.base_url,
        "query": config.query,
        "courts": list(config.courts),
        "delay": config.delay,
        "jitter": config.jitter,
    }
    config.manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Infrastructure helpers
# ---------------------------------------------------------------------------
def open_fetcher(config: RunConfig) -> Fetcher:
    """Build a rate-limited :class:`Fetcher` from a run configuration."""
    limiter = RateLimiter(config.delay, config.jitter)
    return Fetcher(make_client(config), limiter, config.max_attempts)


def install_cancel_handler() -> CancelToken:
    """Install a SIGINT handler that flips a :class:`CancelToken`.

    A first Ctrl-C requests a graceful stop; the current item finishes its
    cleanup and the run exits without corrupting data. A second Ctrl-C restores
    Python's default handler, so if the graceful stop is wedged in a long
    backoff or socket read the user can still force-quit with ``KeyboardInterrupt``.
    """
    token = CancelToken()

    def _handle(signum: int, frame: object) -> None:
        token.cancel()
        # Escalate: the next Ctrl-C raises KeyboardInterrupt immediately.
        signal.signal(signal.SIGINT, signal.default_int_handler)

    signal.signal(signal.SIGINT, _handle)
    return token


def _append_error(config: RunConfig, reason: str, detail: str) -> None:
    """Append a durable, timestamped line to the run's error log."""
    line = f"[{datetime.now(UTC).isoformat()}] {reason} | {detail}\n"
    with config.error_log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _progress(console: Console) -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )


# ---------------------------------------------------------------------------
# Phase 1: listing
# ---------------------------------------------------------------------------
def run_listing(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    *,
    max_pages: int | None = None,
    console: Console | None = None,
) -> int:
    """Populate the database from the paginated search results.

    Args:
        config: The run configuration.
        fetcher: HTTP facade.
        repo: Open repository.
        max_pages: Optional cap on pages fetched (useful for testing/sampling).
        console: Rich console for progress output.

    Returns:
        The number of rows recorded across all fetched pages.
    """
    console = console or Console()
    from courts_scraper.query import search_url

    first_url = search_url(config.base_url, config.query, page=0)
    html = fetcher.get_text(first_url)
    first_rows = parse_search_page(html, config.base_url, page=0)
    total_results = parse_result_count(html)

    # Markup-drift guard: a genuinely empty search still advertises a result
    # count. Zero rows *and* no parseable count means the page structure the
    # parser depends on has probably changed -- fail loudly rather than record a
    # silent, empty "successful" run.
    if not first_rows and total_results is None:
        raise ListingError(
            "no result rows and no result count found on the first page -- "
            "the site's HTML structure may have changed"
        )

    last_page = parse_last_page(html)
    # Cross-check the pager against the advertised count: a truncated pager
    # (numbered links only up to a window) would otherwise undercount the true
    # last page and silently skip later judgments.
    if total_results and first_rows:
        pages_from_count = -(-total_results // len(first_rows)) - 1  # ceil - 1
        last_page = max(last_page, pages_from_count)
    if max_pages is not None:
        last_page = min(last_page, max_pages - 1)

    console.print(
        f"Search reports [bold]{total_results or '?'}[/] results "
        f"across pages 0..{last_page}."
    )

    recorded = _record_rows(first_rows, repo)
    with _progress(console) as progress:
        task = progress.add_task("Listing pages", total=last_page + 1)
        progress.advance(task)
        for page in range(1, last_page + 1):
            page_html = fetcher.get_text(
                search_url(config.base_url, config.query, page=page)
            )
            rows = parse_search_page(page_html, config.base_url, page)
            recorded += _record_rows(rows, repo)
            progress.advance(task)

    return recorded


def _record_rows(rows: list[ListRow], repo: Repository) -> int:
    for row in rows:
        repo.upsert_listing(row)
    return len(rows)


# ---------------------------------------------------------------------------
# Phase 2a: metadata
# ---------------------------------------------------------------------------
def run_metadata(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    *,
    cancel: CancelToken,
    limit: int | None = None,
    console: Console | None = None,
) -> None:
    """Scrape each pending row's view page for authoritative metadata.

    Every document is fetched from its own view page so the archived metadata is
    faithful per opinion. A row with no Neutral Citation is recorded as an error
    (durably logged) and skipped -- the run continues.

    When ``limit`` is set, only the first N pending rows are resolved, so
    ``--limit`` sampling runs stay fast by matching metadata work to the capped
    number of downloads; leave it ``None`` for a full crawl.
    """
    console = console or Console()
    pending = list(repo.iter_pending_metadata())
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        return

    with _progress(console) as progress:
        task = progress.add_task("Fetching metadata", total=len(pending))
        for row in pending:
            if cancel.cancelled:
                console.print("[yellow]Cancelled during metadata phase.[/]")
                break
            _resolve_metadata(config, fetcher, repo, row, console)
            progress.advance(task)


def _resolve_metadata(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    row: sqlite3.Row,
    console: Console,
) -> None:
    record_id = row["id"]
    # Each document has its own view page; we fetch every one so the archived
    # metadata is faithful to that specific opinion, even when several opinions
    # share one Neutral Citation.
    html = fetcher.get_text(row["view_url"])
    meta = parse_view_page(html, config.base_url)

    try:
        filename = pdf_filename(
            meta.neutral_citation, row["judge"], taken=repo.taken_filenames()
        )
    except MissingCitationError as exc:
        repo.record_meta_error(record_id, str(exc), meta)
        _append_error(
            config, "no_neutral_citation", f"{row['title']} | {row['view_url']}"
        )
        console.print(f"[yellow]No citation:[/] {row['title']} (skipped)")
        return

    repo.record_metadata(record_id, meta, filename)


# ---------------------------------------------------------------------------
# Phase 2b: downloads
# ---------------------------------------------------------------------------
def run_downloads(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    *,
    cancel: CancelToken,
    limit: int | None = None,
    console: Console | None = None,
) -> None:
    """Download every ready PDF into the run's ``pdfs/`` folder.

    Startup first sweeps orphaned ``.part`` files. Each download is atomic and
    checksum-verified; an already-verified file is skipped on resume.
    """
    console = console or Console()
    swept = sweep_partials(config.pdf_dir)
    if swept:
        console.print(f"Removed {swept} orphaned partial file(s).")

    pending = list(repo.iter_pending_downloads())
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        console.print("Nothing to download.")
        return

    with _progress(console) as progress:
        task = progress.add_task("Downloading PDFs", total=len(pending))
        for row in pending:
            if cancel.cancelled:
                console.print("[yellow]Cancelled -- partial file discarded.[/]")
                break
            try:
                _download_row(config, fetcher, repo, row, cancel, console)
            except DownloadCancelled:
                console.print("[yellow]Cancelled -- partial file discarded.[/]")
                break
            progress.advance(task)


def _download_row(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    row: sqlite3.Row,
    cancel: CancelToken,
    console: Console,
) -> None:
    # Only rows with download_status in (pending, error) reach here (see
    # ``Repository.iter_pending_downloads``); completed rows are filtered out by
    # the query, so there is no resume shortcut to apply at this point. A row
    # whose file exists but is still ``pending`` (crash between publish and DB
    # commit) has no stored checksum to trust, so it is simply re-downloaded.
    record_id = row["id"]
    target = config.pdf_dir / row["filename"]

    try:
        result = download_pdf(
            fetcher,
            row["pdf_url"],
            target,
            cancel=cancel,
            max_attempts=config.max_attempts,
        )
    except DownloadCancelled:
        raise  # handled by the caller as a graceful stop
    except Exception as exc:  # record and continue the run
        repo.record_download_error(record_id, f"{type(exc).__name__}: {exc}")
        _append_error(
            config, "download_failed", f"{row['filename']} | {row['pdf_url']}"
        )
        console.print(f"[red]Failed:[/] {row['filename']}")
        return

    repo.record_download(record_id, result.sha256, result.size)
