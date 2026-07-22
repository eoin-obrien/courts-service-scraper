"""Command-line interface for the courts.ie judgments scraper.

Commands (grouped in ``--help`` via panels; flat to type):

* ``fetch``      -- start a new run, or resume an existing one. Replaces the old
  ``list`` / ``download`` / ``run`` trio; ``--list-only`` does the listing phase
  alone. Resume is "just run it again" (``--run-dir``/``--latest``).
* ``update``     -- maintain a canonical run: fetch only newly-published judgments
  (and, with ``--revalidate``, re-check downloaded ones for server-side changes).
* ``status``     -- print progress counts for an existing run (``--json`` twin).
* ``runs``       -- list runs under the data directory (``--json`` twin).
* ``export``     -- one run -> Frictionless Data Package (``--json`` twin).
* ``corpus``     -- all runs -> citable BagIt bundle (``--json`` twin).
* ``dictionary`` -- print the export field data dictionary.

Scraping commands are deliberately not eager: if ``--court`` is omitted you are
prompted to pick courts, and every scrape shows its scale and asks for
confirmation first. Pass ``--yes`` to skip the confirmation for unattended runs.
All network commands are polite by default (5s spacing, single worker) and fully
resumable. Press Ctrl-C once to stop cleanly (exit 0); a second Ctrl-C aborts (130).

``--data-dir`` is a global option: it must precede the subcommand
(``courts-scraper --data-dir DIR runs``) or come from ``COURTS_SCRAPER_DATA``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
from courts_scraper.progress import ProgressReporter, RunFinished, RunStarted
from courts_scraper.progress.select import select_reporter
from courts_scraper.query import Court
from courts_scraper.run import (
    PHASE_DOWNLOADS,
    PHASE_LISTING,
    PHASE_METADATA,
    PHASE_REVALIDATE,
    ListingError,
    ListingPreview,
    RunLocked,
    build_run_config,
    estimate_seconds,
    finalize_listing,
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
from courts_scraper.runs import RunInfo, list_runs

app = typer.Typer(
    help="Research scraper for Courts Service of Ireland judgments.",
    no_args_is_help=True,
    add_completion=False,
    epilog=(
        "Typical flow:\n"
        "  courts-scraper fetch -c supreme     # crawl + download\n"
        "  courts-scraper status --latest      # check progress\n"
        "  courts-scraper update --latest      # later: pull new judgments\n"
        "  courts-scraper export --latest      # publish a data package\n\n"
        "A wide terminal shows a live dashboard (ETA, current item, next-request\n"
        "countdown, outage state); piped/cron/narrow/--quiet runs fall back to plain\n"
        "status lines (or silence).\n\n"
        "Exit codes: 0 success (incl. clean first-Ctrl-C stop) | 1 outage/error | "
        "2 bad usage | 130 second Ctrl-C | 143 SIGTERM."
    ),
)
console = Console()
err_console = Console(stderr=True)

DEFAULT_USER_AGENT = f"courts-scraper/{__version__} (research tool)"


@dataclass
class AppState:
    """Session-wide options resolved on the app callback."""

    data_dir: Path
    quiet: bool = False


def _state(ctx: typer.Context) -> AppState:
    obj = ctx.obj
    assert isinstance(obj, AppState)  # set by the callback for every invocation
    return obj


def _run_console(state: AppState) -> Console:
    """The console progress/chrome is written to (silenced by ``--quiet``)."""
    return Console(quiet=True) if state.quiet else console


def _engine_console(state: AppState, *, json_out: bool) -> Console:
    """Console for engine progress/cancel/error output.

    In ``--json`` mode this must stay OFF stdout so the command emits a single
    JSON document: send it to stderr (or silence it under ``--quiet``). Otherwise
    it is the normal stdout progress console.
    """
    if json_out:
        return Console(quiet=True) if state.quiet else err_console
    return _run_console(state)


def _build_reporter(
    state: AppState, config: RunConfig, *, json_out: bool = False
) -> ProgressReporter:
    """Pick the progress reporter for this run's output surface."""
    return select_reporter(
        _engine_console(state, json_out=json_out),
        quiet=state.quiet,
        delay=config.delay,
        jitter=config.jitter,
        prefer_plain=json_out,
    )


def _est_requests_fresh(preview: ListingPreview, *, include_downloads: bool) -> int:
    """Estimate total requests for a fresh crawl (listing + metadata + downloads)."""
    known = (
        preview.total_results
        if preview.total_results is not None
        else len(preview.first_rows)
    )
    return preview.total_pages + (2 * known if include_downloads else 0)


def _est_requests_resume(counts: dict[str, int]) -> int:
    """Estimate remaining requests for a resume/update (metadata + downloads)."""
    return (
        counts["meta_pending"] + counts["download_pending"] + counts["download_error"]
    )


def _run_finished(repo: Repository) -> RunFinished:
    """Build the terminal :class:`RunFinished` event from the run's counts."""
    counts = dict(repo.counts())
    complete = (
        counts["meta_pending"] == 0 and counts["download_done"] >= counts["meta_ok"]
    )
    return RunFinished(counts, 0.0, incomplete=not complete)


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
    ctx: typer.Context,
    data_dir: Annotated[
        Path,
        typer.Option(
            "--data-dir",
            envvar="COURTS_SCRAPER_DATA",
            help="Root folder for run data. As a global option it must come BEFORE "
            "the subcommand: `courts-scraper --data-dir DIR <command>`. Also read "
            "from COURTS_SCRAPER_DATA.",
        ),
    ] = Path("data"),
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Suppress progress and narration chrome."),
    ] = False,
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
    ctx.obj = AppState(data_dir=data_dir, quiet=quiet)


# Shared option annotations -------------------------------------------------
CourtOption = Annotated[
    list[str],
    typer.Option(
        "--court",
        "-c",
        help="Court to include (repeatable): supreme, court_of_appeal, high. "
        "If omitted (and no run selector given), you are prompted to choose.",
    ),
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
    typer.Option("--latest", help="Act on the most recent run without prompting."),
]
ListOnlyOption = Annotated[
    bool,
    typer.Option(
        "--list-only",
        help="Record the search results only (no metadata/PDFs). New runs only.",
    ),
]
DelayOption = Annotated[
    float, typer.Option("--delay", help="Minimum seconds between requests.")
]
JitterOption = Annotated[
    float, typer.Option("--jitter", help="Max extra random seconds per request.")
]
MaxPagesOption = Annotated[
    int | None,
    typer.Option("--max-pages", help="Cap on search pages (testing).", min=1),
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
JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Emit machine-readable JSON to stdout (diagnostics go to stderr).",
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


def _matching_runs(data_dir: Path, courts: tuple[Court, ...]) -> list[RunInfo]:
    """Readable runs whose court set exactly matches ``courts`` (newest first)."""
    want = frozenset(c.value for c in courts)
    return [
        run
        for run in list_runs(data_dir)
        if run.readable and frozenset(run.courts) == want
    ]


def _preview_or_exit(
    config: RunConfig, max_pages: int | None
) -> tuple[ListingPreview, Fetcher]:
    """Fetch the first page for the confirmation, exiting cleanly on drift."""
    fetcher = open_fetcher(config)
    try:
        preview = preview_listing(config, fetcher, max_pages=max_pages)
    except ListingError as exc:
        err_console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    return preview, fetcher


def _print_politeness(
    config: RunConfig, est_seconds: float, *, upper_bound: bool, out: Console = console
) -> None:
    prefix = "up to ~" if upper_bound else "~"
    out.print(
        f"Politeness: {config.delay:g}s + {config.jitter:g}s jitter  "
        f"->  {prefix}{format_duration(est_seconds)}."
    )


def _confirm_new_scrape(
    config: RunConfig,
    preview: ListingPreview,
    *,
    include_downloads: bool,
    yes: bool,
    out: Console = console,
) -> None:
    """Show the scale of a fresh crawl and confirm before creating the run."""
    count = preview.total_results
    known = count if count is not None else len(preview.first_rows)
    requests = preview.total_pages + (2 * known if include_downloads else 0)
    scale = f"{count:,}" if count is not None else "an unknown number of"
    scope = "list + download" if include_downloads else "list only"
    out.print(
        f"[bold]{', '.join(config.courts)}[/]: {scale} results "
        f"across {preview.total_pages} pages ({scope})."
    )
    _print_politeness(
        config,
        estimate_seconds(requests, delay=config.delay, jitter=config.jitter),
        upper_bound=include_downloads,
        out=out,
    )
    out.print(f"Run folder: [bold]{config.run_dir}[/]")
    prompts.confirm_proceed(assume_yes=yes)


def _breadcrumb(config: RunConfig) -> None:
    """Point the user at the exact command to resume an incomplete run."""
    console.print(
        f"Resume with: [bold]courts-scraper fetch --run-dir {config.run_dir}[/]"
    )


def _is_incomplete(config: RunConfig) -> bool:
    with Repository(config.db_path) as repo:
        complete, _ = _resume_summary(repo.counts())
    return not complete


# fetch: the merged crawl command ------------------------------------------
@app.command("fetch", rich_help_panel="Crawl")
def fetch_cmd(
    ctx: typer.Context,
    court: CourtOption = [],  # noqa: B006 -- Typer requires a literal default
    run_dir: RunDirOption = None,
    latest: LatestOption = False,
    list_only: ListOnlyOption = False,
    delay: DelayOption = 5.0,
    jitter: JitterOption = 2.0,
    max_pages: MaxPagesOption = None,
    limit: LimitOption = None,
    max_attempts: AttemptsOption = 4,
    timeout: TimeoutOption = 60.0,
    yes: YesOption = False,
    user_agent: UserAgentOption = DEFAULT_USER_AGENT,
) -> None:
    """Start a new scrape, or resume an existing run (resumable, cancellable)."""
    state = _state(ctx)
    resume_selector = run_dir is not None or latest

    if court and resume_selector:
        raise typer.BadParameter(
            "--court starts a new run; it cannot be combined with --run-dir/--latest."
        )
    if list_only and resume_selector:
        raise typer.BadParameter(
            "--list-only starts a new run; it cannot be combined with "
            "--run-dir/--latest (a resume continues into downloads)."
        )
    if list_only and limit is not None:
        raise typer.BadParameter(
            "--limit caps downloads, which --list-only does not perform."
        )

    net = _NetParams(delay, jitter, max_attempts, timeout, user_agent)

    if court:
        _fetch_new(
            state,
            _resolve_courts(court),
            net,
            list_only=list_only,
            max_pages=max_pages,
            limit=limit,
            yes=yes,
        )
        return
    if resume_selector:
        resolved = _resolve_run_dir(run_dir, state.data_dir, latest)
        _fetch_resume(state, resolved, net, limit=limit, yes=yes)
        return

    # No selector: interactive front door, or a clear error in a non-TTY.
    if not prompts.is_interactive():
        raise typer.BadParameter(
            "no run selected -- pass --court to start a new run, or "
            "--latest/--run-dir to resume."
        )
    # --list-only always means a fresh listing run, so it never offers a resume
    # (resuming ignores --list-only and would silently start downloading).
    resumable = (
        []
        if list_only
        else [r for r in list_runs(state.data_dir) if r.readable and not r.is_complete]
    )
    chosen = prompts.select_new_or_run(resumable) if resumable else None
    if chosen is None:
        # The user explicitly chose "start new" here, so don't re-offer to resume
        # a same-court incomplete run inside _fetch_new (that double-prompts).
        _fetch_new(
            state,
            prompts.select_courts(),
            net,
            list_only=list_only,
            max_pages=max_pages,
            limit=limit,
            yes=yes,
            offer_resume=False,
        )
    else:
        _fetch_resume(state, chosen.path, net, limit=limit, yes=yes)


@dataclass(frozen=True)
class _NetParams:
    """The politeness/transport options shared by the crawl commands."""

    delay: float
    jitter: float
    max_attempts: int
    timeout: float
    user_agent: str


def _fetch_new(
    state: AppState,
    courts: tuple[Court, ...],
    net: _NetParams,
    *,
    list_only: bool,
    max_pages: int | None,
    limit: int | None,
    yes: bool,
    offer_resume: bool = True,
) -> None:
    """Start a new run, guarding against duplicate/complete matches first.

    ``offer_resume`` is False when the caller (the interactive front door) has
    already let the user choose "start new" over resuming, so re-offering a
    same-court resume here would double-prompt.
    """
    matches = _matching_runs(state.data_dir, courts)
    # A run whose listing was truncated by --max-pages is not a canonical crawl of
    # these courts, so it must not block a real (full) fetch as "already complete".
    if any(run.is_complete and not run.listing_truncated for run in matches):
        done = next(
            run for run in matches if run.is_complete and not run.listing_truncated
        )
        console.print(
            f"A complete run for [bold]{', '.join(c.value for c in courts)}[/] "
            f"already exists ([bold]{done.name}[/]). Use `courts-scraper update` to "
            f"fetch newly-published judgments, or `--run-dir`/`--latest` to use it."
        )
        raise typer.Exit(code=0)
    # Only runs with listed rows are meaningfully resumable. A zero-row match is
    # ambiguous (empty court, or a listing interrupted before its first row) and
    # `is_complete` reports it False while `_resume_summary` reports 0/0 complete;
    # excluding it here avoids offering a dead-end resume or a duplicate run, and
    # lets `fetch -c` fall through to a fresh (re-listing) run.
    incomplete = [run for run in matches if not run.is_complete and run.total > 0]
    if incomplete and offer_resume:
        existing = incomplete[0]
        if prompts.is_interactive():
            if prompts.confirm_resume_existing(existing):
                _fetch_resume(state, existing.path, net, limit=limit, yes=yes)
                return
        else:
            err_console.print(
                f"[yellow]An incomplete run for these courts exists "
                f"([bold]{existing.name}[/]); starting a NEW run. Resume it instead "
                f"with: courts-scraper fetch --run-dir {existing.path}[/]"
            )

    config = build_run_config(
        data_dir=state.data_dir,
        courts=courts,
        delay=net.delay,
        jitter=net.jitter,
        max_attempts=net.max_attempts,
        timeout=net.timeout,
        user_agent=net.user_agent,
    )
    preview, fetcher = _preview_or_exit(config, max_pages)
    _confirm_new_scrape(config, preview, include_downloads=not list_only, yes=yes)

    materialize_run(config)
    cancel = install_cancel_handler()
    reporter = _build_reporter(state, config)
    phases = (
        (PHASE_LISTING,)
        if list_only
        else (PHASE_LISTING, PHASE_METADATA, PHASE_DOWNLOADS)
    )
    est = _est_requests_fresh(preview, include_downloads=not list_only)
    recorded = 0
    # Lock even the fresh run: folder names are only second-precise, so two
    # `fetch -c` for the same courts started in the same second would otherwise
    # share a directory and corrupt one SQLite DB / .part set. The reporter is the
    # outermost context so its live display restores the terminal on any exit.
    try:
        with (
            reporter,
            run_lock(config.run_dir),
            Repository(config.db_path) as repo,
        ):
            fetcher.set_reporter(reporter)
            reporter.emit(RunStarted(config.run_dir.name, config.courts, phases, est))
            recorded = run_listing(
                config, fetcher, repo, preview=preview, reporter=reporter
            )
            # Record listing completeness only once the listing pass has finished,
            # so an interrupted crawl never leaves a manifest that claims "complete".
            finalize_listing(config, preview)
            if not list_only:
                run_metadata(
                    config, fetcher, repo, cancel=cancel, limit=limit, reporter=reporter
                )
                run_downloads(
                    config, fetcher, repo, cancel=cancel, limit=limit, reporter=reporter
                )
            reporter.emit(_run_finished(repo))
    except RunLocked as exc:
        err_console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=1) from exc

    if list_only:
        console.print(f"Recorded [bold]{recorded}[/] rows (listing only).")
        _breadcrumb(config)
        return
    _print_status(config)
    if _is_incomplete(config):
        _breadcrumb(config)


def _fetch_resume(
    state: AppState,
    run_dir: Path,
    net: _NetParams,
    *,
    limit: int | None,
    yes: bool,
) -> None:
    """Resume a run: scrape metadata and download PDFs (resumable, cancellable)."""
    config = _load(run_dir, net)
    with Repository(config.db_path) as repo:
        counts = repo.counts()
        complete, lines = _resume_summary(counts)
        console.print(f"Resuming [bold]{config.run_dir.name}[/].")
        for line in lines:
            console.print(line)
        if complete:
            console.print(
                "This run is complete. Use `courts-scraper update` to fetch "
                "newly-published judgments."
            )
            _print_status(config)
            return
        est_requests = (
            counts["meta_pending"]
            + counts["download_pending"]
            + counts["download_error"]
        )
        if not state.quiet:
            _print_politeness(
                config,
                estimate_seconds(
                    est_requests, delay=config.delay, jitter=config.jitter
                ),
                upper_bound=True,
            )
        prompts.confirm_proceed(assume_yes=yes)

        cancel = install_cancel_handler()
        fetcher = open_fetcher(config)
        reporter = _build_reporter(state, config)
        phases = (PHASE_METADATA, PHASE_DOWNLOADS)
        try:
            with reporter, run_lock(config.run_dir):
                fetcher.set_reporter(reporter)
                reporter.emit(
                    RunStarted(
                        config.run_dir.name,
                        config.courts,
                        phases,
                        _est_requests_resume(dict(counts)),
                    )
                )
                run_metadata(
                    config, fetcher, repo, cancel=cancel, limit=limit, reporter=reporter
                )
                run_downloads(
                    config, fetcher, repo, cancel=cancel, limit=limit, reporter=reporter
                )
                reporter.emit(_run_finished(repo))
        except RunLocked as exc:
            err_console.print(f"[yellow]{exc}[/]")
            raise typer.Exit(code=1) from exc
    _print_status(config)
    if _is_incomplete(config):
        _breadcrumb(config)


@app.command("update", rich_help_panel="Crawl")
def update_cmd(
    ctx: typer.Context,
    run_dir: RunDirOption = None,
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
    json_out: JsonOption = False,
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
    state = _state(ctx)
    _json_selector_guard(run_dir, latest, json_out=json_out)
    if json_out and not yes:
        raise typer.BadParameter(
            "--json needs --yes (it will not prompt for confirmation)."
        )
    msg = err_console if json_out else console
    resolved = _resolve_run_dir(run_dir, state.data_dir, latest)
    config = _load(
        resolved, _NetParams(delay, jitter, max_attempts, timeout, user_agent)
    )

    with Repository(config.db_path) as repo:
        baseline = repo.counts()["total"]
    if baseline == 0:
        raise typer.BadParameter(
            f"{config.run_dir.name} has no baseline listing to update; "
            "start it with `courts-scraper fetch --court supreme` first."
        )

    msg.print(
        f"Checking [bold]{config.run_dir.name}[/] for newly-published judgments..."
    )
    preview, fetcher = _preview_for_update(config, max_pages)
    _confirm_update(config, preview, revalidate=revalidate, yes=yes, out=msg)

    cancel = install_cancel_handler()
    reporter = _build_reporter(state, config, json_out=json_out)
    new_rows = 0
    revisions = 0
    try:
        with reporter, run_lock(config.run_dir), Repository(config.db_path) as repo:
            fetcher.set_reporter(reporter)
            before = repo.counts()["total"]
            phases = (PHASE_LISTING, PHASE_METADATA, PHASE_DOWNLOADS) + (
                (PHASE_REVALIDATE,) if revalidate else ()
            )
            # The re-list dominates a no-change update; add the full re-fetch when
            # --revalidate re-downloads every stored PDF.
            est = preview.total_pages + (
                repo.counts()["download_done"] if revalidate else 0
            )
            reporter.emit(RunStarted(config.run_dir.name, config.courts, phases, est))
            try:
                run_listing(config, fetcher, repo, preview=preview, reporter=reporter)
            except httpx.HTTPError:
                msg.print(
                    "[yellow]Site became unavailable during listing; stopped "
                    "partway. Re-run `update` to continue where it left off.[/]"
                )
                raise typer.Exit(code=1) from None
            # Update may only ever clear a prior truncation (coverage is monotonic);
            # finalize_listing enforces that, so a capped update never mislabels an
            # already-full run as partial.
            finalize_listing(config, preview)
            new_rows = repo.counts()["total"] - before

            run_metadata(
                config, fetcher, repo, cancel=cancel, limit=limit, reporter=reporter
            )
            # Timestamp before downloading so revalidate skips the rows this same
            # run is about to fetch (they need no immediate re-check).
            fetched_before = datetime.now(UTC).isoformat()
            run_downloads(
                config, fetcher, repo, cancel=cancel, limit=limit, reporter=reporter
            )
            if revalidate:
                revisions = revalidate_downloads(
                    config,
                    fetcher,
                    repo,
                    cancel=cancel,
                    limit=limit,
                    fetched_before=fetched_before,
                    reporter=reporter,
                )
            reporter.emit(_run_finished(repo))
    except RunLocked as exc:
        msg.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=1) from exc

    # Reported after the live display has closed so it never interleaves a frame.
    msg.print(f"Re-list found [bold]{new_rows}[/] new judgment(s).")

    with Repository(config.db_path) as repo:
        errors = repo.counts()["download_error"]
    if json_out:
        print(
            json.dumps(
                {
                    "new": new_rows,
                    "revisions": revisions,
                    "errors": errors,
                    "run": config.run_dir.name,
                }
            )
        )
        return
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
        err_console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    except httpx.HTTPError:
        err_console.print(
            "[yellow]Site unavailable; nothing changed. Try again later.[/]"
        )
        raise typer.Exit(code=1) from None
    return preview, fetcher


def _confirm_update(
    config: RunConfig,
    preview: ListingPreview,
    *,
    revalidate: bool,
    yes: bool,
    out: Console = console,
) -> None:
    """Show the update's cost (re-list, and revalidate's full re-fetch) and confirm.

    One gate, before any heavy work: the re-list page cost is known now; the
    new-download cost is only known after the re-list (reported then, as info). The
    ``--revalidate`` cost is loud and explicit because it re-fetches the whole
    corpus -- the server exposes no cache validators, so there is no cheap path.
    """
    count = preview.total_results
    scale = f"{count:,}" if count is not None else "an unknown number of"
    out.print(
        f"[bold]{', '.join(config.courts)}[/]: re-listing {scale} results across "
        f"{preview.total_pages} pages to find new judgments."
    )
    _print_politeness(
        config,
        estimate_seconds(preview.total_pages, delay=config.delay, jitter=config.jitter),
        upper_bound=True,
        out=out,
    )
    if revalidate:
        with Repository(config.db_path) as repo:
            done = repo.counts()["download_done"]
        rev_est = estimate_seconds(done, delay=config.delay, jitter=config.jitter)
        out.print(
            f"[yellow]--revalidate re-downloads every downloaded PDF[/] "
            f"({done:,} files, up to ~{format_duration(rev_est)}) because the "
            f"server exposes no cache validators. Changed documents are versioned; "
            f"unchanged are skipped."
        )
    prompts.confirm_proceed(assume_yes=yes)


@app.command("status", rich_help_panel="Inspect")
def status_cmd(
    ctx: typer.Context,
    run_dir: RunDirOption = None,
    latest: LatestOption = False,
    json_out: JsonOption = False,
) -> None:
    """Print progress counts for a run (picked interactively if not given)."""
    state = _state(ctx)
    _json_selector_guard(run_dir, latest, json_out=json_out)
    resolved = _resolve_run_dir(run_dir, state.data_dir, latest)
    config = _load(resolved, _NetParams(5.0, 2.0, 4, 60.0, DEFAULT_USER_AGENT))
    with Repository(config.db_path) as repo:
        counts = repo.counts()
    if json_out:
        print(json.dumps({**counts, "run": config.run_dir.name}))
        return
    _print_status_table(config.run_dir.name, counts)


@app.command("runs", rich_help_panel="Inspect")
def runs_cmd(ctx: typer.Context, json_out: JsonOption = False) -> None:
    """List existing runs under the data directory with their progress."""
    data_dir = _state(ctx).data_dir
    runs = list_runs(data_dir)
    if json_out:
        print(json.dumps([run.to_dict() for run in runs]))
        return
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


@app.command("export", rich_help_panel="Publish")
def export_cmd(
    ctx: typer.Context,
    run_dir: RunDirOption = None,
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
    json_out: JsonOption = False,
) -> None:
    """Export a run to a Frictionless Data Package (CSV + JSON + optional Parquet)."""
    _json_selector_guard(run_dir, latest, json_out=json_out)
    resolved = _resolve_run_dir(run_dir, _state(ctx).data_dir, latest)
    out_dir = out if out is not None else resolved / "export"
    formats = [token.strip() for token in fmt.split(",") if token.strip()]
    if not formats:
        raise typer.BadParameter("--format must name at least one format.")
    try:
        result = export_run(resolved, out_dir, formats)
    except (ExportError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_out:
        print(
            json.dumps(
                {
                    "record_count": result.record_count,
                    "out_dir": str(out_dir),
                    "files": [path.name for path in result.files],
                    "formats": formats,
                }
            )
        )
        return
    console.print(
        f"Exported [bold]{result.record_count}[/] records to [bold]{out_dir}[/]:"
    )
    for path in result.files:
        console.print(f"  {path.name}")


@app.command("dictionary", rich_help_panel="Publish")
def dictionary_cmd(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write to this file instead of stdout."),
    ] = None,
) -> None:
    """Print (or write) the export data dictionary, generated from the schema."""
    markdown = data_dictionary_markdown()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        # Confirmation to stderr so a piped stdout stays clean.
        err_console.print(f"Wrote data dictionary to [bold]{out}[/].")
    else:
        # Plain stdout so it can be piped to a file; bypass Rich markup.
        print(markdown, end="")


@app.command("corpus", rich_help_panel="Publish")
def corpus_cmd(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output bag folder (default: <data-dir>/corpus)."),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option("--format", "-f", help="Comma-separated: csv, json, parquet."),
    ] = "csv,json",
    json_out: JsonOption = False,
) -> None:
    """Merge all runs into one citable BagIt corpus (dedup + fixity + datasheet)."""
    data_dir = _state(ctx).data_dir
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

    if json_out:
        print(
            json.dumps(
                {
                    "record_count": result.record_count,
                    "run_count": len(run_dirs),
                    "out_dir": str(out_dir),
                    "conflicts": len(result.conflicts),
                    "missing_pdfs": len(result.missing_pdfs),
                    "unverified_versions": len(result.unverified_versions),
                }
            )
        )
        return

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
    if result.unverified_versions:
        console.print(
            f"[yellow]{len(result.unverified_versions)} superseded version(s) "
            f"unverifiable[/] (missing or digest mismatch) and omitted from the bag."
        )
    console.print("Verify fixity with any BagIt tool (manifest-sha256.txt).")


def _json_selector_guard(run_dir: Path | None, latest: bool, *, json_out: bool) -> None:
    """In --json mode, refuse to open the interactive run picker.

    Otherwise ``status --json`` / ``export --json`` with no selector would prompt
    on stdout in a TTY, breaking the "stdout is one JSON document" contract.
    """
    if json_out and run_dir is None and not latest:
        raise typer.BadParameter(
            "--json needs an explicit run: pass --run-dir or --latest "
            "(it will not open an interactive picker)."
        )


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


def _load(run_dir: Path, net: _NetParams) -> RunConfig:
    try:
        return load_run_config(
            run_dir,
            delay=net.delay,
            jitter=net.jitter,
            max_attempts=net.max_attempts,
            timeout=net.timeout,
            user_agent=net.user_agent,
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _print_status(config: RunConfig) -> None:
    with Repository(config.db_path) as repo:
        counts = repo.counts()
    _print_status_table(config.run_dir.name, counts)


def _print_status_table(run_name: str, counts: dict[str, int]) -> None:
    """Print a human-readable run summary (not a raw dump of status keys)."""
    total = counts["total"]
    meta_ok = counts["meta_ok"]
    table = Table(title=f"Run status: {run_name}")
    table.add_column("Stage")
    table.add_column("Progress", justify="right")

    meta_note = []
    if counts["meta_pending"]:
        meta_note.append(f"{counts['meta_pending']:,} to fetch")
    if counts["meta_error"]:
        meta_note.append(f"{counts['meta_error']:,} skipped")
    table.add_row(
        "Metadata resolved",
        f"{meta_ok:,}/{total:,}" + (f"  ({', '.join(meta_note)})" if meta_note else ""),
    )

    dl_note = (
        f"  ({counts['download_error']:,} to retry)" if counts["download_error"] else ""
    )
    table.add_row(
        "PDFs downloaded",
        f"{counts['download_done']:,}/{meta_ok:,}{dl_note}",
    )
    console.print(table)


if __name__ == "__main__":
    app()
