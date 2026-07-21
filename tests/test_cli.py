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


def test_version_flag():
    result = runner.invoke(app, ["--version"], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "courts-scraper" in _clean(result.output)


def test_user_agent_option_is_available():
    result = runner.invoke(app, ["list", "--help"], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "--user-agent" in _clean(result.output)


def test_empty_user_agent_is_rejected(tmp_path):
    # The callback rejects a blank UA during parsing, before any network work.
    result = runner.invoke(
        app,
        [
            "list",
            "--court",
            "supreme",
            "--user-agent",
            "   ",
            "--yes",
            "--data-dir",
            str(tmp_path),
        ],
        env=_WIDE_ENV,
    )
    assert result.exit_code != 0
    assert "user-agent" in _clean(result.output).lower()
    assert not any(tmp_path.iterdir())  # no run folder created


def test_runs_command_lists_runs(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    result = runner.invoke(app, ["runs", "--data-dir", str(data)], env=_WIDE_ENV)
    assert result.exit_code == 0
    assert "20260101T000000Z__supreme" in _clean(result.output)


def test_runs_command_empty(tmp_path):
    result = runner.invoke(
        app, ["runs", "--data-dir", str(tmp_path / "none")], env=_WIDE_ENV
    )
    assert result.exit_code == 0
    assert "No runs found" in _clean(result.output)


def test_download_without_run_dir_non_interactive_errors(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme")
    result = runner.invoke(app, ["download", "--data-dir", str(data)], env=_WIDE_ENV)
    assert result.exit_code != 0
    assert "run-dir" in _clean(result.output).lower()


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
