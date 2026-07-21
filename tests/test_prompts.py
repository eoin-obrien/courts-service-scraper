import pytest
import typer

from courts_scraper import prompts


def test_select_courts_without_terminal_errors(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    with pytest.raises(typer.BadParameter, match="no --court"):
        prompts.select_courts()


def test_confirm_proceed_assume_yes_skips_prompt(monkeypatch):
    # assume_yes must not even consult the terminal.
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    prompts.confirm_proceed(assume_yes=True)  # returns without raising


def test_confirm_proceed_without_terminal_requires_yes(monkeypatch):
    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    with pytest.raises(typer.BadParameter, match="pass --yes"):
        prompts.confirm_proceed(assume_yes=False)


def test_select_run_empty_errors():
    with pytest.raises(typer.BadParameter, match="no existing runs"):
        prompts.select_run([])


def test_select_run_without_terminal_errors(monkeypatch, tmp_path):
    from courts_scraper.runs import RunInfo

    monkeypatch.setattr(prompts, "is_interactive", lambda: False)
    run = RunInfo(
        path=tmp_path / "r",
        courts=("Supreme Court",),
        created=None,
        total=1,
        done=0,
        error=0,
        readable=True,
    )
    with pytest.raises(typer.BadParameter, match="run-dir"):
        prompts.select_run([run])
