from courts_scraper.parse_list import (
    parse_last_page,
    parse_result_count,
    parse_search_page,
)


def test_parses_one_hundred_rows(search_html, base_url):
    rows = parse_search_page(search_html, base_url, page=0)
    assert len(rows) == 100


def test_first_row_fields(search_html, base_url):
    row = parse_search_page(search_html, base_url, page=0)[0]
    assert row.title == "O'Donnell -v- Dublin City Council"
    assert row.court == "Supreme Court"
    assert row.judge == "Woulfe J."
    assert row.date_delivered == "2026-07-02"
    assert row.date_uploaded == "2026-07-02"
    assert row.view_url.startswith(f"{base_url}/view/Judgments/")
    assert "/acc/alfresco/" in row.pdf_url
    assert "#" not in row.pdf_url  # fragment stripped
    assert row.document_uuid == "a1ae62e5-606c-4360-872e-be27b2f91a03"
    assert row.collection_uuid == "294acbd9-f777-42cb-bc12-e13ed434a4cd"


def test_sibling_opinions_share_collection(search_html, base_url):
    rows = parse_search_page(search_html, base_url, page=0)
    woulfe, hogan = rows[0], rows[1]
    assert woulfe.collection_uuid == hogan.collection_uuid
    assert woulfe.document_uuid != hogan.document_uuid
    assert woulfe.judge != hogan.judge


def test_result_count(search_html):
    assert parse_result_count(search_html) == 2561


def test_last_page(search_html):
    assert parse_last_page(search_html) == 25


def test_page_number_recorded(search_html, base_url):
    rows = parse_search_page(search_html, base_url, page=3)
    assert all(r.page == 3 for r in rows)
