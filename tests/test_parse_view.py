import pytest

from courts_scraper.naming import MissingCitationError, pdf_filename
from courts_scraper.parse_view import parse_view_page


def test_extracts_neutral_citation(view_html, base_url):
    meta = parse_view_page(view_html, base_url)
    assert meta.neutral_citation == "[2026] IESC 36"


def test_extracts_key_fields(view_html, base_url):
    meta = parse_view_page(view_html, base_url)
    assert meta.fields["Court"] == "Supreme Court"
    assert meta.fields["Record Number"] == "89/2025"
    assert meta.fields["Date Delivered"] == "02 July 2026"
    assert meta.fields["Result"] == "Allow Appeal"
    assert "Composition of the Court" in meta.fields


def test_records_supplementary_documents(view_html, base_url):
    meta = parse_view_page(view_html, base_url)
    labels = " ".join(d.label for d in meta.supplementary)
    assert meta.supplementary  # signed copies, memo, summary, docx
    assert "memo" in labels or "summary" in labels


def test_missing_citation_is_none(view_no_citation_html, base_url):
    meta = parse_view_page(view_no_citation_html, base_url)
    assert meta.neutral_citation is None


def test_missing_citation_blocks_filename(view_no_citation_html, base_url):
    meta = parse_view_page(view_no_citation_html, base_url)
    with pytest.raises(MissingCitationError):
        pdf_filename(meta.neutral_citation, "Woulfe J.")
