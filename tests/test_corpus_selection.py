"""Tests for interactive/explicit run selection in the corpus command."""

from __future__ import annotations

import re

import pytest
import typer
from typer.testing import CliRunner

from courts_scraper.cli import _resolve_corpus_runs, app
from courts_scraper.runs import list_runs

runner = CliRunner()
_WIDE_ENV = {"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI.sub("", text)


def _two_runs(make_run_dir, tmp_path):
    a = make_run_dir(tmp_path, "20260101T000000Z__supreme", done=1, total=1)
    b = make_run_dir(tmp_path, "20260102T000000Z__supreme", done=1, total=1)
    readable = [r for r in list_runs(tmp_path) if r.readable]
    return a, b, readable


def test_default_includes_all_readable_runs(make_run_dir, tmp_path):
    _a, _b, readable = _two_runs(make_run_dir, tmp_path)
    dirs = _resolve_corpus_runs(readable, [], select=False, json_out=False)
    assert len(dirs) == 2


def test_explicit_run_dir_narrows_to_those_runs(make_run_dir, tmp_path):
    a, _b, readable = _two_runs(make_run_dir, tmp_path)
    dirs = _resolve_corpus_runs(readable, [a], select=False, json_out=False)
    assert [p.resolve() for p in dirs] == [a.resolve()]


def test_unknown_run_dir_is_rejected(make_run_dir, tmp_path):
    _a, _b, readable = _two_runs(make_run_dir, tmp_path)
    with pytest.raises(typer.BadParameter):
        _resolve_corpus_runs(
            readable, [tmp_path / "nope"], select=False, json_out=False
        )


def test_select_and_run_dir_are_mutually_exclusive(make_run_dir, tmp_path):
    a, _b, readable = _two_runs(make_run_dir, tmp_path)
    with pytest.raises(typer.BadParameter):
        _resolve_corpus_runs(readable, [a], select=True, json_out=False)


def test_select_with_json_is_rejected(make_run_dir, tmp_path):
    _a, _b, readable = _two_runs(make_run_dir, tmp_path)
    with pytest.raises(typer.BadParameter):
        _resolve_corpus_runs(readable, [], select=True, json_out=True)


def test_select_without_a_terminal_is_rejected(make_run_dir, tmp_path):
    # The test session is non-interactive, so --select must fail clearly rather
    # than hang, steering the user to explicit --run-dir folders.
    _a, _b, readable = _two_runs(make_run_dir, tmp_path)
    with pytest.raises(typer.BadParameter):
        _resolve_corpus_runs(readable, [], select=True, json_out=False)


def test_corpus_select_json_conflict_via_cli(make_run_dir, tmp_path):
    _two_runs(make_run_dir, tmp_path)
    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "corpus", "--select", "--json"],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 2
    assert "select" in _clean(result.output).lower()


def test_corpus_builds_from_explicit_run_dirs(make_run_dir, tmp_path):
    a, _b, _readable = _two_runs(make_run_dir, tmp_path)
    out = tmp_path / "corpus"
    result = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "corpus", "--run-dir", str(a), "--out", str(out)],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 0, result.output
    # Only the one selected run contributed; the bag exists.
    assert "1 run(s)" in _clean(result.output)
    assert (out / "bagit.txt").exists()
