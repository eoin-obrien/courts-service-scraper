"""Documentation + descriptor-validity guards (T7/T8).

Keeps the committed data dictionary in sync with the schema, and validates the
hand-rolled Data Package descriptor against the Frictionless spec.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from courts_scraper.db import Repository
from courts_scraper.export import data_dictionary_markdown, export_run
from courts_scraper.models import JudgmentMeta, ListRow

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_committed_data_dictionary_is_in_sync():
    committed = (_REPO_ROOT / "docs" / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
    assert committed == data_dictionary_markdown(), (
        "docs/DATA_DICTIONARY.md is stale -- regenerate with "
        "`courts-scraper dictionary --out docs/DATA_DICTIONARY.md`."
    )


def _seed_run(run_dir: Path) -> None:
    (run_dir / "manifest.json").write_text(
        json.dumps({"base_url": "https://ww2.courts.ie", "courts": ["Supreme Court"]}),
        encoding="utf-8",
    )
    with Repository(run_dir / "judgments.sqlite") as repo:
        repo.upsert_listing(
            ListRow(
                page=0,
                title="O'Donnell -v- DCC",
                court="Supreme Court",
                judge="Woulfe J.",
                date_delivered="2026-07-02",
                date_uploaded="2026-07-02",
                view_url="https://ww2.courts.ie/view/d1",
                pdf_url="https://ww2.courts.ie/d1.pdf",
                collection_uuid="col-1",
                document_uuid="d1",
            )
        )
        (row,) = list(repo.iter_pending_metadata())
        repo.record_metadata(
            row["id"],
            JudgmentMeta(
                neutral_citation="[2026] IESC 36",
                fields={
                    "Composition of the Court": "A C.J.;B J.",
                    "Status": "Approved",
                    "Result": "Allow Appeal",
                },
            ),
            "2026_IESC_36_Woulfe-J.pdf",
        )
        repo.record_download(
            row["id"], sha256="deadbeef", size=1024, content_type="application/pdf"
        )


@pytest.mark.skipif(
    importlib.util.find_spec("frictionless") is None,
    reason="frictionless not installed",
)
def test_datapackage_is_frictionless_valid(tmp_path):
    from frictionless import Package

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _seed_run(run_dir)
    out = tmp_path / "export"
    export_run(run_dir, out, formats=("csv",))

    report = Package(str(out / "datapackage.json")).validate()
    assert report.valid, report.flatten(["type", "note"])
