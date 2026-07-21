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
