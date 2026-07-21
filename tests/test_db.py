import pytest

from courts_scraper.db import Repository
from courts_scraper.models import JudgmentMeta, ListRow


def _row(pdf_url: str, judge: str = "Woulfe J.", collection: str = "col-1") -> ListRow:
    return ListRow(
        page=0,
        title="O'Donnell -v- Dublin City Council",
        court="Supreme Court",
        judge=judge,
        date_delivered="2026-07-02",
        date_uploaded="2026-07-02",
        view_url=f"https://ww2.courts.ie/view/Judgments/doc/{collection}/x.pdf/pdf",
        pdf_url=pdf_url,
        collection_uuid=collection,
        document_uuid="doc-" + pdf_url[-1],
    )


@pytest.fixture
def repo(tmp_path):
    with Repository(tmp_path / "judgments.sqlite") as repository:
        yield repository


def test_upsert_is_idempotent(repo):
    row = _row("https://x/a.pdf")
    repo.upsert_listing(row)
    repo.upsert_listing(row)  # second insert must not duplicate
    assert repo.counts()["total"] == 1


def test_listing_then_metadata_then_download_flow(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())

    meta = JudgmentMeta(neutral_citation="[2026] IESC 36", fields={"Court": "Supreme"})
    repo.record_metadata(row["id"], meta, "2026_IESC_36_Woulfe-J.pdf")

    assert not list(repo.iter_pending_metadata())
    (ready,) = list(repo.iter_pending_downloads())
    assert ready["filename"] == "2026_IESC_36_Woulfe-J.pdf"

    repo.record_download(ready["id"], sha256="abc", size=123)
    # A completed download is not offered again -> resume skips it.
    assert not list(repo.iter_pending_downloads())
    counts = repo.counts()
    assert counts["download_done"] == 1


def test_meta_error_excludes_from_downloads(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    repo.record_meta_error(row["id"], "no_neutral_citation")
    assert not list(repo.iter_pending_downloads())
    assert repo.counts()["meta_error"] == 1


def test_download_error_is_retried(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    repo.record_metadata(
        row["id"], JudgmentMeta(neutral_citation="[2026] IESC 36"), "f.pdf"
    )
    (ready,) = list(repo.iter_pending_downloads())
    repo.record_download_error(ready["id"], "boom")
    # Errored downloads remain eligible for a retry on the next run.
    assert [r["id"] for r in repo.iter_pending_downloads()] == [ready["id"]]


def test_taken_filenames(repo):
    repo.upsert_listing(_row("https://x/a.pdf"))
    (row,) = list(repo.iter_pending_metadata())
    repo.record_metadata(
        row["id"],
        JudgmentMeta(neutral_citation="[2026] IESC 36"),
        "2026_IESC_36_Woulfe-J.pdf",
    )
    assert repo.taken_filenames() == {"2026_IESC_36_Woulfe-J.pdf"}
