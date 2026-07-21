from typer.testing import CliRunner

from courts_scraper.cli import app

runner = CliRunner()


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "courts-scraper" in result.output


def test_user_agent_option_is_available():
    result = runner.invoke(app, ["list", "--help"])
    assert result.exit_code == 0
    assert "--user-agent" in result.output


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
    )
    assert result.exit_code != 0
    assert "user-agent" in result.output.lower()
    assert not any(tmp_path.iterdir())  # no run folder created
