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
