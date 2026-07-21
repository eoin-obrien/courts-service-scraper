"""Command-line interface for the courts.ie judgments scraper.

Commands:

* ``list``     -- start a new run and populate the search-results database.
* ``download`` -- resume a run: scrape view-page metadata and download PDFs.
* ``run``      -- convenience: ``list`` then ``download`` in one invocation.
* ``status``   -- print progress counts for an existing run folder.

Scraping commands are deliberately not eager: if ``--court`` is omitted you are
prompted to pick courts, and every scrape shows its scale and asks for
confirmation first. Pass ``--yes`` to skip the confirmation for unattended runs.
All network commands are polite by default (5s spacing, single worker) and fully
resumable. Press Ctrl-C once to stop cleanly without corrupting data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from courts_scraper import __version__, prompts
from courts_scraper.db import Repository
from courts_scraper.http import Fetcher
from courts_scraper.models import RunConfig
from courts_scraper.query import Court
from courts_scraper.run import (
    ListingError,
    ListingPreview,
    build_run_config,
    estimate_seconds,
    format_duration,
    install_cancel_handler,
    load_run_config,
    materialize_run,
    open_fetcher,
    preview_listing,
    run_downloads,
    run_listing,
    run_metadata,
)

app = typer.Typer(
    help="Research scraper for Courts Service of Ireland judgments.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_USER_AGENT = f"courts-scraper/{__version__} (research tool)"


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"courts-scraper {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Research scraper for Courts Service of Ireland judgments."""


# Shared option annotations -------------------------------------------------
CourtOption = Annotated[
    list[str],
    typer.Option(
        "--court",
        "-c",
        help="Court to include (repeatable): supreme, court_of_appeal, high. "
        "If omitted, you are prompted to choose.",
    ),
]
DataDirOption = Annotated[
    Path, typer.Option("--data-dir", help="Root folder for run data.")
]
RunDirOption = Annotated[
    Path, typer.Option("--run-dir", help="Existing run folder to resume.")
]
DelayOption = Annotated[
    float, typer.Option("--delay", help="Minimum seconds between requests.")
]
JitterOption = Annotated[
    float, typer.Option("--jitter", help="Max extra random seconds per request.")
]
MaxPagesOption = Annotated[
    int | None, typer.Option("--max-pages", help="Cap on search pages (testing).")
]
LimitOption = Annotated[
    int | None, typer.Option("--limit", help="Cap on PDFs to download (testing).")
]
AttemptsOption = Annotated[
    int, typer.Option("--max-attempts", help="Retry attempts per request.")
]
TimeoutOption = Annotated[
    float, typer.Option("--timeout", help="Per-request timeout in seconds.")
]
YesOption = Annotated[
    bool,
    typer.Option(
        "--yes", "-y", help="Skip the confirmation prompt (for unattended runs)."
    ),
]


def _resolve_courts(tokens: list[str]) -> tuple[Court, ...]:
    try:
        return tuple(Court.from_token(t) for t in tokens)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _courts_or_prompt(tokens: list[str]) -> tuple[Court, ...]:
    """Resolve explicit ``--court`` tokens, or prompt with a multiselect."""
    return _resolve_courts(tokens) if tokens else prompts.select_courts()


def _preview_or_exit(
    config: RunConfig, max_pages: int | None
) -> tuple[ListingPreview, Fetcher]:
    """Fetch the first page for the confirmation, exiting cleanly on drift."""
    fetcher = open_fetcher(config)
    try:
        preview = preview_listing(config, fetcher, max_pages=max_pages)
    except ListingError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    return preview, fetcher


def _print_politeness(
    config: RunConfig, est_seconds: float, *, upper_bound: bool
) -> None:
    prefix = "up to ~" if upper_bound else "~"
    console.print(
        f"Politeness: {config.delay:g}s + {config.jitter:g}s jitter  "
        f"->  {prefix}{format_duration(est_seconds)}."
    )


def _confirm_new_scrape(
    config: RunConfig, preview: ListingPreview, *, include_downloads: bool, yes: bool
) -> None:
    """Show the scale of a fresh crawl and confirm before creating the run."""
    count = preview.total_results
    known = count if count is not None else len(preview.first_rows)
    requests = preview.total_pages + (2 * known if include_downloads else 0)
    scale = f"{count:,}" if count is not None else "an unknown number of"
    scope = "list + download" if include_downloads else "list only"
    console.print(
        f"[bold]{', '.join(config.courts)}[/]: {scale} results "
        f"across {preview.total_pages} pages ({scope})."
    )
    _print_politeness(
        config,
        estimate_seconds(requests, delay=config.delay, jitter=config.jitter),
        upper_bound=include_downloads,
    )
    console.print(f"Run folder: [bold]{config.run_dir}[/]")
    prompts.confirm_proceed(assume_yes=yes)


@app.command("list")
def list_cmd(
    court: CourtOption = [],  # noqa: B006 -- Typer requires a literal default
    data_dir: DataDirOption = Path("data"),
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    max_pages: MaxPagesOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
    yes: YesOption = False,
) -> None:
    """Start a new run and record the paginated search results."""
    config = _build_config(court, data_dir, delay, jitter, max_attempts, timeout)
    preview, fetcher = _preview_or_exit(config, max_pages)
    _confirm_new_scrape(config, preview, include_downloads=False, yes=yes)

    materialize_run(config)
    with Repository(config.db_path) as repo:
        recorded = run_listing(config, fetcher, repo, preview=preview, console=console)
    console.print(
        f"Recorded [bold]{recorded}[/] rows. "
        f"Resume downloads with:\n  courts-scraper download "
        f"--run-dir {config.run_dir}"
    )


@app.command("run")
def run_cmd(
    court: CourtOption = [],  # noqa: B006 -- Typer requires a literal default
    data_dir: DataDirOption = Path("data"),
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    max_pages: MaxPagesOption = None,
    limit: LimitOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
    yes: YesOption = False,
) -> None:
    """Run both phases: list the results, then download every PDF."""
    config = _build_config(court, data_dir, delay, jitter, max_attempts, timeout)
    preview, fetcher = _preview_or_exit(config, max_pages)
    _confirm_new_scrape(config, preview, include_downloads=True, yes=yes)

    materialize_run(config)
    cancel = install_cancel_handler()
    with Repository(config.db_path) as repo:
        run_listing(config, fetcher, repo, preview=preview, console=console)
        run_metadata(config, fetcher, repo, cancel=cancel, limit=limit, console=console)
        run_downloads(
            config, fetcher, repo, cancel=cancel, limit=limit, console=console
        )
    _print_status(config)


@app.command("download")
def download_cmd(
    run_dir: RunDirOption,
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    limit: LimitOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
    yes: YesOption = False,
) -> None:
    """Resume a run: scrape metadata and download PDFs (resumable, cancellable)."""
    config = _load(run_dir, delay, jitter, max_attempts, timeout)
    with Repository(config.db_path) as repo:
        counts = repo.counts()
        remaining = counts["total"] - counts["download_done"]
        if remaining <= 0:
            console.print("Nothing left to do -- all downloads complete.")
            _print_status(config)
            return
        est_requests = (
            counts["meta_pending"]
            + counts["download_pending"]
            + counts["download_error"]
        )
        console.print(f"Resuming [bold]{config.run_dir.name}[/].")
        console.print(
            f"{remaining:,} judgment(s) left to process "
            f"({counts['download_done']:,} already done)."
        )
        _print_politeness(
            config,
            estimate_seconds(est_requests, delay=config.delay, jitter=config.jitter),
            upper_bound=True,
        )
        prompts.confirm_proceed(assume_yes=yes)

        cancel = install_cancel_handler()
        fetcher = open_fetcher(config)
        run_metadata(config, fetcher, repo, cancel=cancel, limit=limit, console=console)
        run_downloads(
            config, fetcher, repo, cancel=cancel, limit=limit, console=console
        )
    _print_status(config)


@app.command("status")
def status_cmd(run_dir: RunDirOption) -> None:
    """Print progress counts for an existing run folder."""
    config = _load(run_dir, 5.0, 2.0, 4, 60.0)
    _print_status(config)


def _build_config(
    court: list[str],
    data_dir: Path,
    delay: float,
    jitter: float,
    max_attempts: int,
    timeout: float,
) -> RunConfig:
    courts = _courts_or_prompt(court)
    return build_run_config(
        data_dir=data_dir,
        courts=courts,
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=DEFAULT_USER_AGENT,
    )


def _load(
    run_dir: Path, delay: float, jitter: float, max_attempts: int, timeout: float
) -> RunConfig:
    try:
        return load_run_config(
            run_dir,
            delay=delay,
            jitter=jitter,
            max_attempts=max_attempts,
            timeout=timeout,
            user_agent=DEFAULT_USER_AGENT,
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _print_status(config: RunConfig) -> None:
    with Repository(config.db_path) as repo:
        counts = repo.counts()
    table = Table(title=f"Run status: {config.run_dir.name}")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for key, value in counts.items():
        table.add_row(key, str(value))
    console.print(table)


if __name__ == "__main__":
    app()
