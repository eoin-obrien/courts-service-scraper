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
import os
import signal
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
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
    archive_superseded,
    download_pdf,
    download_to_scratch,
    publish_scratch,
    sha256_of,
    sweep_partials,
)
from courts_scraper.http import Fetcher, build_client
from courts_scraper.models import ListRow, RunConfig
from courts_scraper.naming import (
    MissingCitationError,
    pdf_filename,
    safe_output_path,
)
from courts_scraper.parse_list import (
    parse_last_page,
    parse_result_count,
    parse_search_page,
)
from courts_scraper.parse_view import parse_view_page
from courts_scraper.query import Court, build_query
from courts_scraper.ratelimit import RateLimiter
from courts_scraper.recovery import OutageBreaker, Outcome

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


def build_run_config(
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
    """Build a :class:`RunConfig` for a fresh run without touching the disk.

    The folder name is ``<UTC timestamp>__<court-slug>`` so runs sort
    chronologically and are self-describing. Call :func:`materialize_run` to
    actually create the directories and manifest (done only after the user has
    confirmed, so a declined run leaves no empty folder behind).
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = data_dir / f"{stamp}__{_slugify_courts(courts)}"
    return RunConfig(
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


def materialize_run(config: RunConfig) -> None:
    """Create the run's directories and write its manifest to disk."""
    _prepare_dirs(config)
    _write_manifest(config)


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
    """Build a run config and immediately create its folder and manifest."""
    config = build_run_config(
        data_dir=data_dir,
        courts=courts,
        base_url=base_url,
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=user_agent,
    )
    materialize_run(config)
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


def _write_json_atomic(path: Path, obj: object) -> None:
    """Write ``obj`` as pretty JSON to ``path`` atomically.

    A plain ``write_text`` truncates the file first, so a crash or full disk
    mid-write leaves a torn manifest -- which the next resume/update reads with a
    raw ``json.loads`` and dies on. Write to a sibling temp file, fsync, then
    ``os.replace`` (atomic on POSIX and Windows) so readers only ever see the old
    or the new whole file.
    """
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)  # atomic on POSIX and Windows
    finally:
        tmp.unlink(missing_ok=True)


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
        "user_agent": config.user_agent,
    }
    _write_json_atomic(config.manifest_path, manifest)


def finalize_listing(config: RunConfig, preview: ListingPreview) -> None:
    """Stamp the manifest with the completeness of the listing that just finished.

    Called *after* :func:`run_listing` returns (not at run creation), so the block
    reflects the crawl that actually landed on disk. An interrupted or errored
    listing never reaches here, so the block stays absent -- which readers must
    treat as "not verified complete", never as "full".

    The block records ``complete`` (the listing pass finished), ``truncated``
    (the run covers fewer pages than the site currently advertises), and the page
    counts behind that verdict.

    **Coverage is the largest prefix ever fetched, judged against the current
    total.** Every listing pass fetches a contiguous prefix ``[0, total_pages)``
    from page 0, and :func:`run_listing` only upserts, so the union across passes
    is ``max(this pass, the prior recorded pass)`` pages. Truncation is then that
    coverage against *this* pass's ``pages_available`` -- which matters because the
    result set grows between runs (that is what ``update`` is for). A prior full
    crawl of 27 pages does NOT stay "full" after the site grows to 40 and a capped
    ``update`` re-lists only 30; the run genuinely misses pages 30-39, so it is
    truncated, and this recomputes that rather than trusting the stale verdict.
    """
    try:
        manifest = json.loads(config.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return

    prior_covered = 0
    previous = manifest.get("listing")
    if isinstance(previous, dict) and previous.get("complete") is True:
        prior_pages = previous.get("pages_fetched")
        if isinstance(prior_pages, int):
            prior_covered = prior_pages

    # Largest contiguous prefix reached across this pass and any prior one.
    covered = max(preview.total_pages, prior_covered)
    available = preview.pages_available
    truncated = available is not None and covered < available

    manifest["listing"] = {
        "complete": True,
        "truncated": truncated,
        "max_pages": preview.max_pages,
        "pages_fetched": covered,
        "pages_available": available,
    }
    _write_json_atomic(config.manifest_path, manifest)


# ---------------------------------------------------------------------------
# Infrastructure helpers
# ---------------------------------------------------------------------------
def open_fetcher(config: RunConfig) -> Fetcher:
    """Build a rate-limited :class:`Fetcher` from a run configuration."""
    return build_fetcher(
        delay=config.delay,
        jitter=config.jitter,
        max_attempts=config.max_attempts,
        timeout=config.timeout,
        user_agent=config.user_agent,
    )


def build_fetcher(
    *,
    delay: float,
    jitter: float,
    max_attempts: int,
    timeout: float,
    user_agent: str,
) -> Fetcher:
    """Build a :class:`Fetcher` from explicit settings, without a run folder.

    Used to fetch the first search page for the pre-scrape confirmation before
    any run directory is created.
    """
    limiter = RateLimiter(delay, jitter)
    client = build_client(user_agent=user_agent, timeout=timeout)
    return Fetcher(client, limiter, max_attempts)


def format_duration(seconds: float) -> str:
    """Render a rough duration as ``"45s"``, ``"12m"``, or ``"1h 5m"``."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 30 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def estimate_seconds(requests: int, *, delay: float, jitter: float) -> float:
    """Estimate wall-clock seconds for ``requests`` polite requests."""
    return requests * (delay + jitter / 2)


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


class RunLocked(RuntimeError):
    """Raised when another process already holds a run's lock."""


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on a run for the duration of a network command.

    Two processes operating on the same run (an overlapping cron ``update``, or
    ``update`` overlapping a manual ``download``) would collide on the shared
    ``<name>.pdf.part`` scratch files -- interleaving bytes on disk while each
    records the digest of only its own stream, a silent fixity break in an
    archival tool. This takes an exclusive advisory ``flock`` so the second process
    fails fast with a clear message instead. On platforms without ``fcntl`` (e.g.
    Windows) it degrades to a best-effort no-op.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover -- non-POSIX fallback
        yield
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    handle = (run_dir / ".lock").open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunLocked(
                f"another courts-scraper process is already working on "
                f"{run_dir.name}; wait for it to finish or stop it first."
            ) from exc
        yield
    finally:
        handle.close()


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
@dataclass(frozen=True)
class ListingPreview:
    """The parsed first search page plus the shape of the full crawl.

    Built before the pre-scrape confirmation so the user can see the scale
    (result count, page count) before committing to the whole crawl.
    """

    first_html: str
    first_rows: list[ListRow]
    total_results: int | None
    last_page: int  # zero-based, already capped by max_pages
    # Truncation provenance, so a later manifest write can record whether this
    # crawl was deliberately cut short. ``pages_available`` is the full crawl's
    # page count *before* the ``max_pages`` cap; ``None`` means "unknown" (e.g. a
    # synthetic preview in a test) and is treated as "not truncated".
    max_pages: int | None = None
    pages_available: int | None = None

    @property
    def total_pages(self) -> int:
        """Number of pages that will be fetched (1-based count)."""
        return self.last_page + 1

    @property
    def truncated(self) -> bool:
        """Whether ``max_pages`` cut this crawl short of the full result set."""
        if self.pages_available is None:
            return False
        return self.total_pages < self.pages_available


def preview_listing(
    config: RunConfig, fetcher: Fetcher, *, max_pages: int | None = None
) -> ListingPreview:
    """Fetch and parse the first search page to learn the crawl's scale.

    Raises:
        ListingError: If the page has no rows and no result count -- a sign the
            site's HTML structure has changed.
    """
    from courts_scraper.query import search_url

    html = fetcher.get_text(search_url(config.base_url, config.query, page=0))
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
    # The full crawl's size, captured after the count cross-check but before any
    # deliberate cap -- so the manifest can later record whether --max-pages
    # actually cut the crawl short.
    pages_available = last_page + 1
    if max_pages is not None:
        last_page = min(last_page, max_pages - 1)

    return ListingPreview(
        html,
        first_rows,
        total_results,
        last_page,
        max_pages=max_pages,
        pages_available=pages_available,
    )


def run_listing(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    *,
    preview: ListingPreview,
    console: Console | None = None,
) -> int:
    """Populate the database from the paginated search results.

    Args:
        config: The run configuration.
        fetcher: HTTP facade.
        repo: Open repository.
        preview: The already-fetched first page (from :func:`preview_listing`);
            its first page is reused rather than re-fetched.
        console: Rich console for progress output.

    Returns:
        The number of rows recorded across all fetched pages.
    """
    console = console or Console()
    from courts_scraper.query import search_url

    console.print(
        f"Search reports [bold]{preview.total_results or '?'}[/] results "
        f"across pages 0..{preview.last_page}."
    )

    recorded = _record_rows(preview.first_rows, repo)
    with _progress(console) as progress:
        task = progress.add_task("Listing pages", total=preview.total_pages)
        progress.advance(task)
        for page in range(1, preview.last_page + 1):
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

    breaker = OutageBreaker(fetcher, config.base_url, console)
    with _progress(console) as progress:
        task = progress.add_task("Fetching metadata", total=len(pending))
        for row in pending:
            if cancel.cancelled:
                console.print("[yellow]Cancelled during metadata phase.[/]")
                break
            try:
                outcome, exc = breaker.run(
                    partial(_resolve_metadata, config, fetcher, repo, row, console)
                )
            except Exception as err:  # parse/db error -- record and continue
                repo.record_meta_error(row["id"], f"{type(err).__name__}: {err}")
                _append_error(
                    config, "metadata_error", f"{row['title']} | {row['view_url']}"
                )
                console.print(f"[red]Metadata error:[/] {row['title']} (skipped)")
                progress.advance(task)
                continue

            if outcome is Outcome.GAVE_UP:
                console.print("[red]Stopping the metadata phase (site down).[/]")
                break
            if outcome is Outcome.DEFER:
                _append_error(
                    config,
                    "metadata_fetch_failed",
                    f"{row['title']} | {row['view_url']} | {exc}",
                )
                console.print(
                    f"[red]Metadata fetch failed[/] (will retry on resume): "
                    f"{row['title']}"
                )
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

    breaker = OutageBreaker(fetcher, config.base_url, console)
    with _progress(console) as progress:
        task = progress.add_task("Downloading PDFs", total=len(pending))
        for row in pending:
            if cancel.cancelled:
                console.print("[yellow]Cancelled -- partial file discarded.[/]")
                break
            try:
                outcome, exc = breaker.run(
                    partial(_download_row, config, fetcher, repo, row, cancel)
                )
            except DownloadCancelled:
                console.print("[yellow]Cancelled -- partial file discarded.[/]")
                break
            except Exception as err:  # e.g. unsafe filename -- record and continue
                repo.record_download_error(row["id"], f"{type(err).__name__}: {err}")
                _append_error(
                    config, "download_failed", f"{row['filename']} | {row['pdf_url']}"
                )
                console.print(f"[red]Failed:[/] {row['filename']}")
                progress.advance(task)
                continue

            if outcome is Outcome.GAVE_UP:
                console.print("[red]Stopping the download phase (site down).[/]")
                break
            if outcome is Outcome.DEFER:
                repo.record_download_error(row["id"], f"{exc}")
                _append_error(
                    config, "download_failed", f"{row['filename']} | {row['pdf_url']}"
                )
                console.print(
                    f"[red]Download failed[/] (will retry on resume): {row['filename']}"
                )
            progress.advance(task)


def _download_row(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    row: sqlite3.Row,
    cancel: CancelToken,
) -> None:
    """Download one row's PDF, recording it on success (raises on failure).

    Only rows with ``download_status`` in (pending, error) reach here; completed
    rows are filtered out by the query. A row whose file exists but is still
    ``pending`` (crash between publish and DB commit) has no trusted checksum, so
    it is simply re-downloaded. The filename is validated to stay inside the PDF
    folder before any write (defence-in-depth against a tampered database row).
    """
    target = safe_output_path(config.pdf_dir, row["filename"])
    result = download_pdf(
        fetcher,
        row["pdf_url"],
        target,
        cancel=cancel,
        max_attempts=config.max_attempts,
    )
    repo.record_download(
        row["id"],
        result.sha256,
        result.size,
        last_modified=result.last_modified,
        etag=result.etag,
        content_length=result.content_length,
        content_type=result.content_type,
    )


# ---------------------------------------------------------------------------
# Phase 3 (update --revalidate): re-check downloaded documents for changes
# ---------------------------------------------------------------------------
_SCRATCH_SUFFIX = ".part"  # must match download.sweep_partials so orphans are swept


def revalidate_downloads(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    *,
    cancel: CancelToken,
    limit: int | None = None,
    fetched_before: str | None = None,
    console: Console | None = None,
) -> int:
    """Re-fetch every downloaded PDF and record any content change (Tier 2).

    The Courts Service server exposes no cache validators (no ``ETag``/
    ``Last-Modified``, and conditional requests never ``304``), so detecting a
    change means a full GET of each document -- deliberately opt-in and loud about
    that cost. For each fully-downloaded record, least-recently-checked first:

    * download a fresh copy to scratch and hash it (no publish yet);
    * if the digest matches what we stored, discard the scratch and just stamp the
      row re-checked (bytes and provenance untouched -- unchanged means untouched);
    * if it differs, archive the previous bytes under ``pdfs/versions/<old-sha>.pdf``,
      atomically publish the new bytes over the live file, then record a new
      ``pdf_version`` row -- in that order, so a crash never hides a mutation and no
      verified version is ever overwritten.

    Politeness spacing and the outage circuit-breaker apply to every fetch; the
    least-recently-checked ordering makes a ``--limit`` sweep rotate and an
    interrupted run resume. A per-row fetch failure leaves the good ``done`` file
    and its (unstamped) row untouched, so it is retried first next time.
    ``fetched_before`` (an ISO timestamp) skips rows fetched at/after it, so a
    combined ``update --revalidate`` does not immediately re-download the judgments
    it just fetched. Returns the number of changed documents detected this run.
    """
    console = console or Console()
    swept = sweep_partials(config.pdf_dir)
    if swept:
        console.print(f"Removed {swept} orphaned partial file(s).")

    repo.backfill_pdf_versions()
    targets = list(repo.iter_revalidation_targets(fetched_before=fetched_before))
    if limit is not None:
        targets = targets[:limit]
    if not targets:
        console.print("Nothing to revalidate.")
        return 0

    before = repo.count_revisions()
    breaker = OutageBreaker(fetcher, config.base_url, console)
    with _progress(console) as progress:
        task = progress.add_task("Revalidating PDFs", total=len(targets))
        for row in targets:
            if cancel.cancelled:
                console.print("[yellow]Cancelled during revalidation.[/]")
                break
            try:
                outcome, exc = breaker.run(
                    partial(_revalidate_row, config, fetcher, repo, row, cancel)
                )
            except DownloadCancelled:
                console.print("[yellow]Cancelled -- scratch discarded.[/]")
                break
            except Exception as err:  # unsafe filename etc. -- log, keep row 'done'
                _append_error(
                    config,
                    "revalidate_error",
                    f"{row['filename']} | {row['pdf_url']} | "
                    f"{type(err).__name__}: {err}",
                )
                console.print(f"[red]Revalidate error:[/] {row['filename']} (skipped)")
                progress.advance(task)
                continue

            if outcome is Outcome.GAVE_UP:
                console.print("[red]Stopping revalidation (site down).[/]")
                break
            if outcome is Outcome.DEFER:
                # Leave the row unchecked (last_revalidated_at untouched) so it is
                # retried first next run; the good 'done' file is not disturbed.
                _append_error(
                    config,
                    "revalidate_fetch_failed",
                    f"{row['filename']} | {row['pdf_url']} | {exc}",
                )
                console.print(
                    f"[red]Revalidate fetch failed[/] (will retry on resume): "
                    f"{row['filename']}"
                )
            progress.advance(task)

    return repo.count_revisions() - before


def _revalidate_row(
    config: RunConfig,
    fetcher: Fetcher,
    repo: Repository,
    row: sqlite3.Row,
    cancel: CancelToken,
) -> None:
    """Re-fetch one row's PDF; record a new version iff its bytes changed.

    Never overwrites the live file until the fresh copy is verified and the old
    bytes are safely archived, so a failure or crash leaves the previous verified
    version intact (see :func:`revalidate_downloads` for the ordering rationale).
    The old bytes are archived under *their own* sha256 (read from disk), not the
    DB's stored digest, so the content-addressed name always matches its content
    even if an earlier torn write left the live file and the DB row disagreeing.
    """
    target = safe_output_path(config.pdf_dir, row["filename"])
    scratch = target.with_name(target.name + _SCRATCH_SUFFIX)
    result = download_to_scratch(
        fetcher,
        row["pdf_url"],
        scratch,
        cancel=cancel,
        max_attempts=config.max_attempts,
    )

    stored = None if row["sha256"] is None else str(row["sha256"])
    live_sha = sha256_of(target) if target.exists() else None
    if result.sha256 == stored and live_sha == stored:
        # Genuinely unchanged and the on-disk file already matches: keep the
        # archived bytes and provenance exactly as they were, just record the check.
        scratch.unlink(missing_ok=True)
        repo.mark_revalidated(row["id"])
        return
    if result.sha256 == stored:
        # The server still serves the recorded bytes, but the live file is missing
        # or was left mismatched by an earlier torn write. Self-heal by publishing
        # the verified fresh copy over it so disk matches the DB again.
        publish_scratch(scratch, target)
        repo.mark_revalidated(row["id"])
        return

    # Changed: preserve the *actual* old bytes under their true digest (idempotent),
    # publish the new bytes atomically, then make the DB agree -- in that order so a
    # crash never hides a mutation and no archive is named by a digest it lacks.
    old_sha = live_sha if live_sha is not None else (stored or result.sha256)
    old_bytes = target.stat().st_size if target.exists() else (row["bytes"] or 0)
    archive = safe_output_path(config.versions_dir, f"{old_sha}.pdf")
    archive_superseded(target, archive)
    publish_scratch(scratch, target)
    repo.record_new_version(
        row["id"],
        result.sha256,
        result.size,
        archived_filename=f"versions/{old_sha}.pdf",
        old_sha256=old_sha,
        old_bytes=int(old_bytes),
        last_modified=result.last_modified,
        etag=result.etag,
        content_length=result.content_length,
        content_type=result.content_type,
    )
