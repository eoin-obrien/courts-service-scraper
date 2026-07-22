"""Shared test fixtures: access to saved HTML and a common base URL."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://ww2.courts.ie"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def search_html() -> str:
    return _load("search_page.html")


@pytest.fixture
def view_html() -> str:
    return _load("view_page.html")


@pytest.fixture
def view_no_citation_html() -> str:
    return _load("view_no_citation.html")


@pytest.fixture
def base_url() -> str:
    return BASE_URL


@pytest.fixture
def make_run_dir():
    """Factory that builds a run folder with a chosen name and progress."""
    import json

    from courts_scraper.db import Repository
    from courts_scraper.models import JudgmentMeta, ListRow

    def _make(
        data_dir, name, *, courts=("Supreme Court",), done=0, total=1, listing=None
    ):
        run = Path(data_dir) / name
        run.mkdir(parents=True, exist_ok=True)
        # A manifest complete enough for load_run_config (base_url + query), so a
        # run built by this fixture can be resumed/inspected through the CLI.
        # ``listing`` optionally stamps the completeness block a real run would
        # write via finalize_listing.
        manifest = {
            "courts": list(courts),
            "created": name,
            "base_url": BASE_URL,
            "query": {},
        }
        if listing is not None:
            manifest["listing"] = listing
        (run / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        with Repository(run / "judgments.sqlite") as repo:
            for i in range(total):
                repo.upsert_listing(
                    ListRow(
                        page=0,
                        title="X -v- Y",
                        court="Supreme Court",
                        judge=f"J{i}",
                        date_delivered=None,
                        date_uploaded=None,
                        view_url="https://ww2.courts.ie/view/x",
                        pdf_url=f"https://ww2.courts.ie/acc/alfresco/x/{i}.pdf",
                        collection_uuid="c",
                        document_uuid=f"d{i}",
                    )
                )
            rows = list(repo.iter_pending_metadata())
            for i in range(done):
                repo.record_metadata(
                    rows[i]["id"],
                    JudgmentMeta(neutral_citation="[2026] IESC 36"),
                    f"f{i}.pdf",
                )
                repo.record_download(rows[i]["id"], "sha", 1)
        return run

    return _make
