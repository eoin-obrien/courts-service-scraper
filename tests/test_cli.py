import json
import re

from typer.testing import CliRunner

from courts_scraper.cli import app

runner = CliRunner()

# Typer renders help/errors with rich, which colourises and wraps to the
# terminal width. Force a wide, colourless console so assertions do not depend
# on the CI terminal size, and strip any residual ANSI before matching.
_WIDE_ENV = {"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI.sub("", text)


# --- basics ---------------------------------------------------------------


def test_version_flag():
    result = runner.invoke(app, ["--version"], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "courts-scraper" in _clean(result.output)


def test_help_groups_commands_into_panels():
    result = runner.invoke(app, ["--help"], env=_WIDE_ENV)
    out = _clean(result.output)
    assert result.exit_code == 0
    for panel in ("Crawl", "Inspect", "Publish"):
        assert panel in out
    # The "Typical flow" epilog is present.
    assert "Typical flow" in out
    assert "fetch" in out and "dictionary" in out


def test_user_agent_option_is_available():
    result = runner.invoke(app, ["fetch", "--help"], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "--user-agent" in _clean(result.output)


# --- removed commands -----------------------------------------------------


def test_removed_commands_exit_nonzero():
    for name in ("list", "download", "run", "data-dictionary"):
        result = runner.invoke(app, [name, "--help"], env=_WIDE_ENV)
        assert result.exit_code != 0, f"{name} should no longer exist"


# --- fetch dispatch: mutual exclusion & flag rules ------------------------


def test_fetch_court_and_run_dir_are_mutually_exclusive(tmp_path):
    result = runner.invoke(
        app,
        ["fetch", "-c", "supreme", "--run-dir", str(tmp_path / "x")],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 2
    assert "cannot be combined" in _clean(result.output).lower()


def test_fetch_list_only_requires_new_run(tmp_path):
    result = runner.invoke(app, ["fetch", "--latest", "--list-only"], env=_WIDE_ENV)
    assert result.exit_code == 2
    assert "list-only" in _clean(result.output).lower()


def test_fetch_list_only_rejects_limit():
    result = runner.invoke(
        app, ["fetch", "-c", "supreme", "--list-only", "--limit", "5"], env=_WIDE_ENV
    )
    assert result.exit_code == 2
    assert "limit" in _clean(result.output).lower()


def test_fetch_no_selector_non_interactive_errors(tmp_path):
    # No TTY under CliRunner, no selector -> a clear usage error (exit 2).
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "fetch"], env=_WIDE_ENV)
    assert result.exit_code == 2
    assert "no run selected" in _clean(result.output).lower()


def test_empty_user_agent_is_rejected(tmp_path):
    # The callback rejects a blank UA during parsing, before any network work.
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "fetch",
            "--court",
            "supreme",
            "--user-agent",
            "   ",
            "--yes",
        ],
        env=_WIDE_ENV,
    )
    assert result.exit_code != 0
    assert "user-agent" in _clean(result.output).lower()
    assert not any(tmp_path.iterdir())  # no run folder created


# --- Decision-3 guard: fetch -c with an existing matching run -------------


def test_fetch_court_refuses_when_complete_run_exists(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=2, total=2)  # complete
    result = runner.invoke(
        app, ["--data-dir", str(data), "fetch", "-c", "supreme"], env=_WIDE_ENV
    )
    out = _clean(result.output)
    assert result.exit_code == 0
    assert "already exists" in out
    assert "update" in out  # points at the right command
    # No new run folder was created (still just the one).
    assert len([p for p in data.iterdir() if p.is_dir()]) == 1


# --- fetch resume: complete run points at update --------------------------


def test_fetch_latest_on_complete_run_points_at_update(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=2, total=2)
    result = runner.invoke(
        app, ["--data-dir", str(data), "fetch", "--latest"], env=_WIDE_ENV
    )
    out = _clean(result.output)
    assert result.exit_code == 0
    assert "complete" in out.lower()
    assert "update" in out


# --- global --data-dir ----------------------------------------------------


def test_data_dir_must_precede_subcommand(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    # Correct: global option before the subcommand.
    ok = runner.invoke(app, ["--data-dir", str(data), "runs"], env=_WIDE_ENV)
    assert ok.exit_code == 0
    assert "20260101T000000Z__supreme" in _clean(ok.output)
    # Wrong: after the subcommand -> Typer rejects the unknown option.
    bad = runner.invoke(app, ["runs", "--data-dir", str(data)], env=_WIDE_ENV)
    assert bad.exit_code == 2


def test_data_dir_from_envvar(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    env = {**_WIDE_ENV, "COURTS_SCRAPER_DATA": str(data)}
    result = runner.invoke(app, ["runs"], env=env)
    assert result.exit_code == 0
    assert "20260101T000000Z__supreme" in _clean(result.output)


# --- runs ----------------------------------------------------------------


def test_runs_command_lists_runs(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    result = runner.invoke(app, ["--data-dir", str(data), "runs"], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "20260101T000000Z__supreme" in _clean(result.output)


def test_runs_command_empty(tmp_path):
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path / "none"), "runs"], env=_WIDE_ENV
    )
    assert result.exit_code == 0
    assert "No runs found" in _clean(result.output)


def test_runs_json(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    result = runner.invoke(
        app, ["--data-dir", str(data), "runs", "--json"], env=_WIDE_ENV
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list) and len(payload) == 1
    row = payload[0]
    assert set(row) == {
        "name",
        "courts",
        "created",
        "total",
        "done",
        "error",
        "readable",
        "complete",
        "listing_verified",
        "listing_truncated",
        "pages_fetched",
        "pages_available",
        "path",
    }
    assert row["name"] == "20260101T000000Z__supreme"
    # A run built without a listing block reads as unverified, not truncated.
    assert row["listing_verified"] is False
    assert row["listing_truncated"] is False
    assert row["total"] == 2 and row["done"] == 1 and row["complete"] is False


# --- --json never prompts (deterministic contract) -----------------------


def test_status_json_without_selector_errors(tmp_path):
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "status", "--json"], env=_WIDE_ENV
    )
    assert result.exit_code == 2
    assert "--run-dir or --latest" in _clean(result.output)


def test_export_json_without_selector_errors(tmp_path):
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "export", "--json"], env=_WIDE_ENV
    )
    assert result.exit_code == 2
    assert "--run-dir or --latest" in _clean(result.output)


def test_update_json_requires_yes(make_run_dir, tmp_path):
    data = tmp_path / "data"
    run = make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    result = runner.invoke(
        app,
        ["--data-dir", str(data), "update", "--run-dir", str(run), "--json"],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 2
    assert "--yes" in _clean(result.output)


# --- status --------------------------------------------------------------


def test_status_json_shape(make_run_dir, tmp_path):
    data = tmp_path / "data"
    run = make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    result = runner.invoke(
        app,
        ["--data-dir", str(data), "status", "--run-dir", str(run), "--json"],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "total",
        "meta_pending",
        "meta_ok",
        "meta_error",
        "download_pending",
        "download_done",
        "download_error",
        "run",
    }
    assert payload["run"] == "20260101T000000Z__supreme"
    assert payload["total"] == 2


# --- dictionary ----------------------------------------------------------


def test_dictionary_stdout():
    result = runner.invoke(app, ["dictionary"], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "# Data dictionary" in result.stdout


def test_dictionary_out_file_keeps_stdout_clean(tmp_path):
    target = tmp_path / "nested" / "dict.md"
    result = runner.invoke(app, ["dictionary", "--out", str(target)], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert target.is_file()
    assert "# Data dictionary" in target.read_text(encoding="utf-8")
    # Confirmation goes to stderr; stdout stays clean.
    assert result.stdout.strip() == ""


# --- fetch new run (mocked network) ---------------------------------------


def test_fetch_list_only_new_run_records_and_breadcrumbs(
    httpx_mock, search_html, tmp_path
):
    from courts_scraper.query import Court, build_query, search_url
    from courts_scraper.run import DEFAULT_BASE_URL

    page0 = search_url(DEFAULT_BASE_URL, build_query((Court.SUPREME,)), page=0)
    # A single-page (max_pages=1) crawl: preview fetches page 0 and run_listing
    # reuses that preview, so page 0 is requested exactly once.
    httpx_mock.add_response(url=page0, text=search_html)

    data = tmp_path / "data"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data),
            "fetch",
            "-c",
            "supreme",
            "--list-only",
            "--max-pages",
            "1",
            "--delay",
            "0",
            "--jitter",
            "0",
            "--yes",
        ],
        env=_WIDE_ENV,
    )
    out = _clean(result.output)
    assert result.exit_code == 0, out
    assert "listing only" in out.lower()
    assert "Resume with: courts-scraper fetch --run-dir" in out
    # A run folder with a database exists, and no PDFs were downloaded.
    runs = [p for p in data.iterdir() if (p / "judgments.sqlite").is_file()]
    assert len(runs) == 1


def test_truncated_run_does_not_block_a_new_fetch(
    httpx_mock, search_html, make_run_dir, tmp_path
):
    """A fully-downloaded but --max-pages-truncated run is not a canonical crawl,
    so a later real fetch for the same court must not be refused as 'complete'."""
    from courts_scraper.query import Court, build_query, search_url
    from courts_scraper.run import DEFAULT_BASE_URL

    data = tmp_path / "data"
    # Pre-existing run: downloads complete, but the listing was capped.
    make_run_dir(
        data,
        "20260101T000000Z__supreme",
        done=1,
        total=1,
        listing={
            "complete": True,
            "truncated": True,
            "max_pages": 1,
            "pages_fetched": 1,
            "pages_available": 26,
        },
    )

    page0 = search_url(DEFAULT_BASE_URL, build_query((Court.SUPREME,)), page=0)
    httpx_mock.add_response(url=page0, text=search_html)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data),
            "fetch",
            "-c",
            "supreme",
            "--list-only",
            "--max-pages",
            "1",
            "--delay",
            "0",
            "--jitter",
            "0",
            "--yes",
        ],
        env=_WIDE_ENV,
    )
    out = _clean(result.output)
    assert result.exit_code == 0, out
    # Not refused as an existing complete run...
    assert "already exists" not in out
    # ...and a second run folder was created.
    runs = [p for p in data.iterdir() if (p / "judgments.sqlite").is_file()]
    assert len(runs) == 2


# --- export / corpus --json (no network) ----------------------------------


def test_export_json(make_run_dir, tmp_path):
    data = tmp_path / "data"
    run = make_run_dir(data, "20260101T000000Z__supreme", done=1, total=1)
    out_dir = tmp_path / "pkg"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data),
            "export",
            "--run-dir",
            str(run),
            "--out",
            str(out_dir),
            "--format",
            "csv,json",
            "--json",
        ],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 0, _clean(result.output)
    payload = json.loads(result.stdout)
    assert set(payload) == {"record_count", "out_dir", "files", "formats"}
    assert payload["record_count"] == 1
    assert payload["formats"] == ["csv", "json"]
    assert payload["out_dir"] == str(out_dir)
    assert all(isinstance(name, str) for name in payload["files"])


def test_corpus_json(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=1)
    out_dir = tmp_path / "corpus"
    result = runner.invoke(
        app,
        ["--data-dir", str(data), "corpus", "--out", str(out_dir), "--json"],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 0, _clean(result.output)
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "record_count",
        "run_count",
        "out_dir",
        "conflicts",
        "missing_pdfs",
        "unverified_versions",
    }
    assert payload["run_count"] == 1
    assert isinstance(payload["missing_pdfs"], int)


# --- internal helpers -----------------------------------------------------


def test_engine_console_json_never_writes_stdout():
    from pathlib import Path

    from courts_scraper.cli import AppState, _engine_console, console, err_console

    st = AppState(data_dir=Path("data"))
    # --json: engine output must go to stderr, never the stdout console.
    json_console = _engine_console(st, json_out=True)
    assert json_console is err_console
    assert json_console.stderr is True
    # --json + --quiet: silenced entirely.
    quiet = AppState(data_dir=Path("data"), quiet=True)
    assert _engine_console(quiet, json_out=True).quiet is True
    # normal mode: the stdout progress console.
    assert _engine_console(st, json_out=False) is console


def test_resolve_run_dir_explicit(tmp_path):
    from courts_scraper.cli import _resolve_run_dir

    explicit = tmp_path / "foo"
    assert _resolve_run_dir(explicit, tmp_path, latest=False) == explicit


def test_resolve_run_dir_latest(make_run_dir, tmp_path):
    from courts_scraper.cli import _resolve_run_dir

    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme")
    newer = make_run_dir(data, "20260202T000000Z__supreme")
    assert _resolve_run_dir(None, data, latest=True) == newer


def test_matching_runs_by_court_set(make_run_dir, tmp_path):
    from courts_scraper.cli import _matching_runs
    from courts_scraper.query import Court

    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", courts=("Supreme Court",))
    make_run_dir(data, "20260101T000000Z__high", courts=("High Court",))
    matches = _matching_runs(data, (Court.SUPREME,))
    assert [m.name for m in matches] == ["20260101T000000Z__supreme"]


def _plain(lines):
    return re.sub(r"\[/?[^\]]*\]", "", " ".join(lines))


def test_resume_summary_shows_metadata_progress():
    from courts_scraper.cli import _resume_summary

    # 918 metadata resolved, nothing downloaded yet -- the old bug reported this
    # as "0 already done".
    counts = {
        "total": 2561,
        "meta_ok": 918,
        "meta_pending": 1643,
        "meta_error": 0,
        "download_done": 0,
        "download_pending": 2561,
        "download_error": 0,
    }
    complete, lines = _resume_summary(counts)
    text = _plain(lines)

    assert not complete
    assert "918/2,561 resolved" in text
    assert "1,643 to fetch" in text
    assert "0/918 PDFs done" in text


def test_resume_summary_complete_when_all_done():
    from courts_scraper.cli import _resume_summary

    counts = {
        "total": 100,
        "meta_ok": 100,
        "meta_pending": 0,
        "meta_error": 0,
        "download_done": 100,
        "download_pending": 0,
        "download_error": 0,
    }
    complete, lines = _resume_summary(counts)
    assert complete
    assert "complete" in _plain(lines)
