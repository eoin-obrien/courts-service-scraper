"""Command-line interface for the courts.ie judgments scraper.

Commands:

* ``list``     -- start a new run and populate the search-results database.
* ``download`` -- resume a run: scrape view-page metadata and download PDFs.
* ``run``      -- convenience: ``list`` then ``download`` in one invocation.
* ``status``   -- print progress counts for an existing run folder.

All network commands are polite by default (5s spacing, single worker) and
fully resumable. Press Ctrl-C once to stop cleanly without corrupting data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from courts_scraper import __version__
from courts_scraper.db import Repository
from courts_scraper.models import RunConfig
from courts_scraper.query import Court
from courts_scraper.run import (
    install_cancel_handler,
    load_run_config,
    new_run_config,
    open_fetcher,
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
        help="Court to include (repeatable): supreme, court_of_appeal, high.",
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


def _resolve_courts(tokens: list[str]) -> tuple[Court, ...]:
    if not tokens:
        return (Court.SUPREME,)
    try:
        return tuple(Court.from_token(t) for t in tokens)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("list")
def list_cmd(
    court: CourtOption = [],  # noqa: B006 -- Typer requires a literal default
    data_dir: DataDirOption = Path("data"),
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    max_pages: MaxPagesOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
) -> None:
    """Start a new run and record the paginated search results."""
    courts = _resolve_courts(court)
    config = new_run_config(
        data_dir=data_dir,
        courts=courts,
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=DEFAULT_USER_AGENT,
    )
    console.print(f"Run folder: [bold]{config.run_dir}[/]")
    fetcher = open_fetcher(config)
    with Repository(config.db_path) as repo:
        recorded = run_listing(
            config, fetcher, repo, max_pages=max_pages, console=console
        )
    console.print(
        f"Recorded [bold]{recorded}[/] rows. "
        f"Resume downloads with:\n  courts-scraper download "
        f"--run-dir {config.run_dir}"
    )


@app.command("download")
def download_cmd(
    run_dir: RunDirOption,
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    limit: LimitOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
) -> None:
    """Resume a run: scrape metadata and download PDFs (resumable, cancellable)."""
    config = _load(run_dir, delay, jitter, max_attempts, timeout)
    cancel = install_cancel_handler()
    fetcher = open_fetcher(config)
    with Repository(config.db_path) as repo:
        run_metadata(config, fetcher, repo, cancel=cancel, limit=limit, console=console)
        run_downloads(
            config, fetcher, repo, cancel=cancel, limit=limit, console=console
        )
    _print_status(config)


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
) -> None:
    """Run both phases: list the results, then download every PDF."""
    courts = _resolve_courts(court)
    config = new_run_config(
        data_dir=data_dir,
        courts=courts,
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=DEFAULT_USER_AGENT,
    )
    console.print(f"Run folder: [bold]{config.run_dir}[/]")
    cancel = install_cancel_handler()
    fetcher = open_fetcher(config)
    with Repository(config.db_path) as repo:
        run_listing(config, fetcher, repo, max_pages=max_pages, console=console)
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
