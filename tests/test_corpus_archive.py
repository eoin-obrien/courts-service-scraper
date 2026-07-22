"""Tests for corpus serialisation (--archive): single-file bags + checksums."""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from courts_scraper.cli import app
from courts_scraper.corpus import (
    ARCHIVE_FORMAT_CHOICES,
    resolve_archive_format,
    serialize_bag,
)
from courts_scraper.export import ExportError

runner = CliRunner()
_WIDE_ENV = {"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"}


def _bag(tmp_path: Path) -> Path:
    """A minimal bag-shaped directory (contents don't matter for serialisation)."""
    bag = tmp_path / "corpus"
    (bag / "data").mkdir(parents=True)
    (bag / "bagit.txt").write_text("BagIt-Version: 1.0\n", encoding="utf-8")
    (bag / "data" / "x.txt").write_text("hello", encoding="utf-8")
    return bag


def test_resolve_archive_format_aliases_and_case():
    assert resolve_archive_format("tar.gz")[0] == "gztar"
    assert resolve_archive_format("tgz")[0] == "gztar"
    assert resolve_archive_format(".zip")[0] == "zip"
    assert resolve_archive_format("TAR.XZ")[0] == "xztar"
    assert resolve_archive_format("tar.bz2")[0] == "bztar"


def test_resolve_archive_format_unknown_is_rejected():
    with pytest.raises(ExportError):
        resolve_archive_format("rar")


@pytest.mark.parametrize(
    ("fmt", "ext"),
    [
        ("zip", ".zip"),
        ("tar", ".tar"),
        ("tar.gz", ".tar.gz"),
        ("tar.bz2", ".tar.bz2"),
        ("tar.xz", ".tar.xz"),
    ],
)
def test_serialize_bag_every_format(tmp_path, fmt, ext):
    bag = _bag(tmp_path)
    archive, digest = serialize_bag(bag, fmt)

    assert archive.name == "corpus" + ext
    assert archive.exists()
    # The sidecar is sha256sum-compatible and matches the archive bytes.
    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.exists()
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sidecar.read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"


def test_all_choices_serialise():
    # Every advertised choice must actually work in this build.
    assert set(ARCHIVE_FORMAT_CHOICES) == {"zip", "tar", "tar.gz", "tar.bz2", "tar.xz"}


def test_zip_top_level_entry_is_the_bag_dir(tmp_path):
    archive, _ = serialize_bag(_bag(tmp_path), "zip")
    with zipfile.ZipFile(archive) as z:
        tops = {name.split("/")[0] for name in z.namelist()}
    assert tops == {"corpus"}  # BagIt serialisation convention


def test_tar_top_level_entry_is_the_bag_dir(tmp_path):
    archive, _ = serialize_bag(_bag(tmp_path), "tar.gz")
    with tarfile.open(archive) as t:
        tops = {m.name.split("/")[0] for m in t.getmembers()}
    assert tops == {"corpus"}


def test_serialize_missing_dir_is_rejected(tmp_path):
    with pytest.raises(ExportError):
        serialize_bag(tmp_path / "nope", "zip")


def test_serialize_unknown_format_is_rejected(tmp_path):
    with pytest.raises(ExportError):
        serialize_bag(_bag(tmp_path), "rar")


def test_corpus_archive_bad_format_fails_fast(make_run_dir, tmp_path):
    make_run_dir(tmp_path, "20260101T000000Z__supreme", done=1, total=1)
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "corpus", "--archive", "rar"], env=_WIDE_ENV
    )
    assert result.exit_code == 2


def test_corpus_archive_end_to_end(make_run_dir, tmp_path):
    a = make_run_dir(tmp_path, "20260101T000000Z__supreme", done=1, total=1)
    out = tmp_path / "corpus"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "corpus",
            "--run-dir",
            str(a),
            "--out",
            str(out),
            "--archive",
            "tar.gz",
            "--json",
        ],
        env=_WIDE_ENV,
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout.strip().splitlines()[-1])
    assert doc["archive"].endswith("corpus.tar.gz")
    assert len(doc["archive_sha256"]) == 64
    archive = Path(doc["archive"])
    assert archive.exists()
    assert archive.with_name("corpus.tar.gz.sha256").exists()
