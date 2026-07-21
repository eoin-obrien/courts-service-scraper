"""Command-line interface for the courts.ie judgments scraper.

Commands:

* ``list``     -- start a new run and populate the search-results database.
* ``download`` -- resume a run: scrape view-page metadata and download PDFs.
* ``run``      -- convenience: ``list`` then ``download`` in one invocation.
* ``update``   -- maintain a canonical run: fetch only newly-published judgments
  (and, with ``--revalidate``, re-check downloaded ones for server-side changes).
* ``status``   -- print progress counts for an existing run folder.

Scraping commands are deliberately not eager: if ``--court`` is omitted you are
prompted to pick courts, and every scrape shows its scale and asks for
confirmation first. Pass ``--yes`` to skip the confirmation for unattended runs.
All network commands are polite by default (5s spacing, single worker) and fully
resumable. Press Ctrl-C once to stop cleanly without corrupting data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from courts_scraper import __version__, prompts
from courts_scraper.corpus import build_corpus
from courts_scraper.db import Repository
from courts_scraper.export import ExportError, data_dictionary_markdown, export_run
from courts_scraper.http import Fetcher
from courts_scraper.models import RunConfig
from courts_scraper.query import Court
from courts_scraper.run import (
    ListingError,
    ListingPreview,
    RunLocked,
    build_run_config,
    estimate_seconds,
    format_duration,
    install_cancel_handler,
    load_run_config,
    materialize_run,
    open_fetcher,
    preview_listing,
    revalidate_downloads,
    run_downloads,
    run_listing,
    run_lock,
    run_metadata,
)
from courts_scraper.runs import list_runs

app = typer.Typer(
    help="Research scraper for Courts Service of Ireland judgments.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

DEFAULT_USER_AGENT = f"courts-scraper/{__version__} (research tool)"


def _validate_user_agent(value: str) -> str:
    if not value.strip():
        raise typer.BadParameter("--user-agent must not be empty.")
    return value


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
    Path | None,
    typer.Option(
        "--run-dir",
        help="Existing run folder to resume. If omitted, pick one interactively.",
    ),
]
LatestOption = Annotated[
    bool,
    typer.Option("--latest", help="Resume the most recent run without prompting."),
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
UserAgentOption = Annotated[
    str,
    typer.Option(
        "--user-agent",
        callback=_validate_user_agent,
        help="User-Agent header sent with every request.",
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
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
) -> None:
    """Start a new run and record the paginated search results."""
    config = _build_config(
        court, data_dir, delay, jitter, max_attempts, timeout, user_agent
    )
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
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
) -> None:
    """Run both phases: list the results, then download every PDF."""
    config = _build_config(
        court, data_dir, delay, jitter, max_attempts, timeout, user_agent
    )
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
    run_dir: RunDirOption = None,
    data_dir: DataDirOption = Path("data"),
    latest: LatestOption = False,
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    limit: LimitOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
    yes: YesOption = False,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
) -> None:
    """Resume a run: scrape metadata and download PDFs (resumable, cancellable)."""
    resolved = _resolve_run_dir(run_dir, data_dir, latest)
    config = _load(resolved, delay, jitter, max_attempts, timeout, user_agent)
    with Repository(config.db_path) as repo:
        counts = repo.counts()
        complete, lines = _resume_summary(counts)
        console.print(f"Resuming [bold]{config.run_dir.name}[/].")
        for line in lines:
            console.print(line)
        if complete:
            _print_status(config)
            return
        est_requests = (
            counts["meta_pending"]
            + counts["download_pending"]
            + counts["download_error"]
        )
        _print_politeness(
            config,
            estimate_seconds(est_requests, delay=config.delay, jitter=config.jitter),
            upper_bound=True,
        )
        prompts.confirm_proceed(assume_yes=yes)

        cancel = install_cancel_handler()
        fetcher = open_fetcher(config)
        try:
            with run_lock(config.run_dir):
                run_metadata(
                    config, fetcher, repo, cancel=cancel, limit=limit, console=console
                )
                run_downloads(
                    config, fetcher, repo, cancel=cancel, limit=limit, console=console
                )
        except RunLocked as exc:
            console.print(f"[yellow]{exc}[/]")
            raise typer.Exit(code=1) from exc
    _print_status(config)


@app.command("update")
def update_cmd(
    run_dir: RunDirOption = None,
    data_dir: DataDirOption = Path("data"),
    latest: LatestOption = False,
    revalidate: Annotated[
        bool,
        typer.Option(
            "--revalidate",
            help="Also re-download every done PDF to detect (and version) "
            "server-side changes. Costly: the server has no cache validators, so "
            "this is a full re-fetch of the corpus. Opt-in.",
        ),
    ] = False,
    max_pages: MaxPagesOption = None,
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    limit: LimitOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
    yes: YesOption = False,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
) -> None:
    """Fetch newly-published judgments into a canonical run (evergreen maintenance).

    Re-lists the run's fixed search so only genuinely-new judgments become pending,
    then scrapes metadata and downloads just those -- so a scheduled job stops
    re-crawling the whole corpus. With ``--revalidate`` it also re-checks every
    already-downloaded PDF for server-side changes, preserving the previous bytes
    and recording each revision. Resumable, polite, and outage-guarded like the
    other network commands, so it is safe on a cron with ``--yes``.
    """
    resolved = _resolve_run_dir(run_dir, data_dir, latest)
    config = _load(resolved, delay, jitter, max_attempts, timeout, user_agent)

    with Repository(config.db_path) as repo:
        baseline = repo.counts()["total"]
    if baseline == 0:
        raise typer.BadParameter(
            f"{config.run_dir.name} has no baseline listing to update; "
            "start it with `courts-scraper list` or `run` first."
        )

    console.print(
        f"Checking [bold]{config.run_dir.name}[/] for newly-published judgments..."
    )
    preview, fetcher = _preview_for_update(config, max_pages)
    _confirm_update(config, preview, revalidate=revalidate, yes=yes)

    cancel = install_cancel_handler()
    new_rows = 0
    revisions = 0
    try:
        with run_lock(config.run_dir), Repository(config.db_path) as repo:
            before = repo.counts()["total"]
            try:
                run_listing(config, fetcher, repo, preview=preview, console=console)
            except httpx.HTTPError:
                console.print(
                    "[yellow]Site became unavailable during listing; stopped "
                    "partway. Re-run `update` to continue where it left off.[/]"
                )
                raise typer.Exit(code=1) from None
            new_rows = repo.counts()["total"] - before
            console.print(f"Re-list found [bold]{new_rows}[/] new judgment(s).")

            run_metadata(
                config, fetcher, repo, cancel=cancel, limit=limit, console=console
            )
            # Timestamp before downloading so revalidate skips the rows this same
            # run is about to fetch (they need no immediate re-check).
            fetched_before = datetime.now(UTC).isoformat()
            run_downloads(
                config, fetcher, repo, cancel=cancel, limit=limit, console=console
            )
            if revalidate:
                revisions = revalidate_downloads(
                    config,
                    fetcher,
                    repo,
                    cancel=cancel,
                    limit=limit,
                    fetched_before=fetched_before,
                    console=console,
                )
    except RunLocked as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=1) from exc

    tail = " (see pdfs/versions/ for superseded copies)" if revisions else ""
    console.print(
        f"[bold]Update complete:[/] {new_rows} new judgment(s) added, "
        f"{revisions} revision(s) detected{tail}."
    )
    _print_status(config)


def _preview_for_update(
    config: RunConfig, max_pages: int | None
) -> tuple[ListingPreview, Fetcher]:
    """Fetch page 0 for an update, exiting cleanly on drift *or* an outage.

    Unlike :func:`_preview_or_exit`, a network failure here is treated as a
    transient outage (clean "try later" exit) rather than a crash, so a cron
    firing during the site's upload-window downtime degrades gracefully.
    """
    fetcher = open_fetcher(config)
    try:
        preview = preview_listing(config, fetcher, max_pages=max_pages)
    except ListingError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    except httpx.HTTPError:
        console.print("[yellow]Site unavailable; nothing changed. Try again later.[/]")
        raise typer.Exit(code=1) from None
    return preview, fetcher


def _confirm_update(
    config: RunConfig, preview: ListingPreview, *, revalidate: bool, yes: bool
) -> None:
    """Show the update's cost (re-list, and revalidate's full re-fetch) and confirm.

    One gate, before any heavy work: the re-list page cost is known now; the
    new-download cost is only known after the re-list (reported then, as info). The
    ``--revalidate`` cost is loud and explicit because it re-fetches the whole
    corpus -- the server exposes no cache validators, so there is no cheap path.
    """
    count = preview.total_results
    scale = f"{count:,}" if count is not None else "an unknown number of"
    console.print(
        f"[bold]{', '.join(config.courts)}[/]: re-listing {scale} results across "
        f"{preview.total_pages} pages to find new judgments."
    )
    _print_politeness(
        config,
        estimate_seconds(preview.total_pages, delay=config.delay, jitter=config.jitter),
        upper_bound=True,
    )
    if revalidate:
        with Repository(config.db_path) as repo:
            done = repo.counts()["download_done"]
        rev_est = estimate_seconds(done, delay=config.delay, jitter=config.jitter)
        console.print(
            f"[yellow]--revalidate re-downloads every downloaded PDF[/] "
            f"({done:,} files, up to ~{format_duration(rev_est)}) because the "
            f"server exposes no cache validators. Changed documents are versioned; "
            f"unchanged are skipped."
        )
    prompts.confirm_proceed(assume_yes=yes)


@app.command("status")
def status_cmd(
    run_dir: RunDirOption = None,
    data_dir: DataDirOption = Path("data"),
    latest: LatestOption = False,
) -> None:
    """Print progress counts for a run (picked interactively if not given)."""
    resolved = _resolve_run_dir(run_dir, data_dir, latest)
    config = _load(resolved, 5.0, 2.0, 4, 60.0, DEFAULT_USER_AGENT)
    _print_status(config)


@app.command("runs")
def runs_cmd(data_dir: DataDirOption = Path("data")) -> None:
    """List existing runs under the data directory with their progress."""
    runs = list_runs(data_dir)
    if not runs:
        console.print(f"No runs found under [bold]{data_dir}[/].")
        return
    table = Table(title=f"Runs in {data_dir}")
    table.add_column("Run")
    table.add_column("Courts")
    table.add_column("Created")
    table.add_column("Downloaded", justify="right")
    table.add_column("Errors", justify="right")
    for run in runs:
        downloaded = f"{run.done}/{run.total}" if run.readable else "unreadable"
        table.add_row(
            run.name,
            ", ".join(run.courts) or "?",
            (run.created or "")[:19],
            downloaded,
            str(run.error) if run.readable else "-",
        )
    console.print(table)


@app.command("export")
def export_cmd(
    run_dir: RunDirOption = None,
    data_dir: DataDirOption = Path("data"),
    latest: LatestOption = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output folder (default: <run>/export)."),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Comma-separated formats: csv, json, parquet.",
        ),
    ] = "csv,json",
) -> None:
    """Export a run to a Frictionless Data Package (CSV + JSON + optional Parquet)."""
    resolved = _resolve_run_dir(run_dir, data_dir, latest)
    out_dir = out if out is not None else resolved / "export"
    formats = [token.strip() for token in fmt.split(",") if token.strip()]
    if not formats:
        raise typer.BadParameter("--format must name at least one format.")
    try:
        result = export_run(resolved, out_dir, formats)
    except (ExportError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"Exported [bold]{result.record_count}[/] records to [bold]{out_dir}[/]:"
    )
    for path in result.files:
        console.print(f"  {path.name}")


@app.command("data-dictionary")
def data_dictionary_cmd(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write to this file instead of stdout."),
    ] = None,
) -> None:
    """Print (or write) the export data dictionary, generated from the schema."""
    markdown = data_dictionary_markdown()
    if out is not None:
        out.write_text(markdown, encoding="utf-8")
        console.print(f"Wrote data dictionary to [bold]{out}[/].")
    else:
        # Plain stdout so it can be piped to a file; bypass Rich markup.
        print(markdown, end="")


@app.command("corpus")
def corpus_cmd(
    data_dir: DataDirOption = Path("data"),
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output bag folder (default: <data-dir>/corpus)."),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option("--format", "-f", help="Comma-separated: csv, json, parquet."),
    ] = "csv,json",
) -> None:
    """Merge all runs into one citable BagIt corpus (dedup + fixity + datasheet)."""
    run_dirs = [run.path for run in list_runs(data_dir) if run.readable]
    if not run_dirs:
        raise typer.BadParameter(f"no readable runs under {data_dir}.")
    out_dir = out if out is not None else data_dir / "corpus"
    formats = [token.strip() for token in fmt.split(",") if token.strip()]
    if not formats:
        raise typer.BadParameter("--format must name at least one format.")
    try:
        result = build_corpus(run_dirs, out_dir, formats=formats)
    except (ExportError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(
        f"Corpus: [bold]{result.record_count}[/] records from "
        f"{len(run_dirs)} run(s) -> [bold]{out_dir}[/]."
    )
    if result.conflicts:
        console.print(
            f"[yellow]{len(result.conflicts)} content conflict(s)[/] "
            f"(same document, differing PDF) -- see data/snapshot.json."
        )
    if result.missing_pdfs:
        console.print(
            f"[yellow]{len(result.missing_pdfs)} PDF(s) missing[/] on disk "
            f"and omitted from the bag."
        )
    console.print("Verify fixity with any BagIt tool (manifest-sha256.txt).")


def _resolve_run_dir(run_dir: Path | None, data_dir: Path, latest: bool) -> Path:
    """Resolve which run to act on: explicit path, newest, or interactive pick."""
    if run_dir is not None:
        return run_dir
    if latest:
        runs = list_runs(data_dir)
        if not runs:
            raise typer.BadParameter(f"no runs found under {data_dir}.")
        return runs[0].path
    return prompts.select_run(list_runs(data_dir)).path


def _resume_summary(counts: dict[str, int]) -> tuple[bool, list[str]]:
    """Summarise a resumed run's progress across both phases.

    Returns ``(complete, lines)``. ``complete`` is True only when there is no
    metadata left to fetch and every resolvable PDF has been downloaded -- so a
    run that has resolved metadata but downloaded nothing is *not* reported as
    "nothing to do" (the previous bug that hid metadata progress).
    """
    total, meta_ok, meta_pending, meta_error, dl_done, dl_error = (
        counts["total"],
        counts["meta_ok"],
        counts["meta_pending"],
        counts["meta_error"],
        counts["download_done"],
        counts["download_error"],
    )

    if meta_pending == 0 and dl_done >= meta_ok:
        return True, ["Nothing left to do -- this run is complete."]

    meta_extras = []
    if meta_pending:
        meta_extras.append(f"{meta_pending:,} to fetch")
    if meta_error:
        meta_extras.append(f"{meta_error:,} skipped")
    meta_line = f"Metadata:  [bold]{meta_ok:,}[/]/{total:,} resolved"
    if meta_extras:
        meta_line += f" ({', '.join(meta_extras)})"

    dl_line = f"Downloads: [bold]{dl_done:,}[/]/{meta_ok:,} PDFs done"
    if dl_error:
        dl_line += f" ({dl_error:,} to retry)"

    return False, [meta_line + ".", dl_line + "."]


def _build_config(
    court: list[str],
    data_dir: Path,
    delay: float,
    jitter: float,
    max_attempts: int,
    timeout: float,
    user_agent: str,
) -> RunConfig:
    courts = _courts_or_prompt(court)
    return build_run_config(
        data_dir=data_dir,
        courts=courts,
        delay=delay,
        jitter=jitter,
        max_attempts=max_attempts,
        timeout=timeout,
        user_agent=user_agent,
    )


def _load(
    run_dir: Path,
    delay: float,
    jitter: float,
    max_attempts: int,
    timeout: float,
    user_agent: str,
) -> RunConfig:
    try:
        return load_run_config(
            run_dir,
            delay=delay,
            jitter=jitter,
            max_attempts=max_attempts,
            timeout=timeout,
            user_agent=user_agent,
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
