"""Interactive prompts for court selection and operation confirmation.

The scraper is often run unattended (long overnight crawls), so every prompt is
terminal-aware:

* In an interactive terminal, the user is prompted.
* Without a terminal (CI, cron, a pipe), prompts never block. A confirmation is
  satisfied only by ``--yes``; a missing court selection is a clear error rather
  than a hang.
"""

from __future__ import annotations

import sys

import questionary
import typer

from courts_scraper.query import Court


def is_interactive() -> bool:
    """Return whether both stdin and stdout are attached to a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def select_courts() -> tuple[Court, ...]:
    """Prompt for one or more courts with a checkbox multiselect.

    Supreme Court is pre-selected as the most common choice.

    Raises:
        typer.BadParameter: If there is no interactive terminal (the caller
            must pass ``--court`` explicitly in that case).
        typer.Abort: If the user cancels or selects nothing.
    """
    if not is_interactive():
        raise typer.BadParameter(
            "no --court given and no interactive terminal to prompt in; "
            "pass --court explicitly (e.g. --court supreme)."
        )

    choices = [
        questionary.Choice(
            title=court.value, value=court, checked=court is Court.SUPREME
        )
        for court in Court
    ]
    selected: list[Court] | None = questionary.checkbox(
        "Select courts to scrape:", choices=choices
    ).ask()

    if not selected:  # None (Ctrl-C) or [] (confirmed with nothing checked)
        raise typer.Abort()
    return tuple(selected)


def confirm_proceed(*, assume_yes: bool) -> None:
    """Require confirmation before a scraping operation begins.

    The caller is expected to have already printed a summary of what is about to
    happen; this only handles the yes/no gate.

    Args:
        assume_yes: If true, skip the prompt (the ``--yes`` flag).

    Raises:
        typer.BadParameter: If confirmation is required but there is no
            interactive terminal (the caller must pass ``--yes``).
        typer.Abort: If the user declines.
    """
    if assume_yes:
        return
    if not is_interactive():
        raise typer.BadParameter(
            "refusing to start a scrape without confirmation in a "
            "non-interactive session; pass --yes to proceed."
        )
    if not questionary.confirm("Proceed?", default=False).ask():
        raise typer.Abort()
