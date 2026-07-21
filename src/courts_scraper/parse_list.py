"""Parsing of the paginated search results page.

The results live in a single ``table.alfresco-table`` whose rows each describe
one judgment document with six cells:

    Date Delivered | Title (view link) | (PDF link) | Court | Judge | Date Uploaded

Both the judgment's view-page URL and a direct PDF-download URL are present in
the row, so phase 1 captures everything needed to download later, plus enough
to fetch richer metadata in phase 2.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from courts_scraper.dates import parse_irish_date
from courts_scraper.models import ListRow

_COUNT_RE = re.compile(r"(\d[\d,]*)\s+results?", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]page=(\d+)")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _absolute(base_url: str, href: str) -> str:
    """Join ``href`` onto ``base_url``, drop any fragment and encode spaces.

    The site emits hrefs containing literal spaces (e.g. ``.../Woulfe J.pdf``);
    these are percent-encoded so the stored URL is directly requestable.
    """
    joined = urljoin(base_url, href.split("#", 1)[0])
    return joined.replace(" ", "%20")


def _uuids(url: str) -> tuple[str, str]:
    """Return ``(document_uuid, collection_uuid)`` from a view URL.

    A view URL has the shape ``/view/Judgments/<document>/<collection>/...``.
    Missing ids are returned as empty strings rather than raising.
    """
    found = _UUID_RE.findall(urlsplit(url).path)
    document = found[0] if len(found) >= 1 else ""
    collection = found[1] if len(found) >= 2 else ""
    return document, collection


def _cell_text(cell: Tag) -> str:
    return cell.get_text(" ", strip=True)


def _cell_href(cell: Tag) -> str | None:
    anchor = cell.find("a", href=True)
    if not isinstance(anchor, Tag):
        return None
    href = anchor.get("href")
    return href if isinstance(href, str) else None


def parse_search_page(html: str, base_url: str, page: int) -> list[ListRow]:
    """Parse one search results page into rows.

    Args:
        html: Raw HTML of the search page.
        base_url: Site origin used to absolutise relative links.
        page: Zero-based page number, stored on each row.

    Returns:
        One :class:`ListRow` per data row. Rows missing a title/view link are
        skipped (defensive against header or spacer rows).
    """
    soup = _soup(html)
    table = soup.find("table", class_="alfresco-table")
    if not isinstance(table, Tag):
        return []

    rows: list[ListRow] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 6:
            continue  # header row (<th>) or malformed row

        view_href = _cell_href(cells[1])
        pdf_href = _cell_href(cells[2])
        if not view_href or not pdf_href:
            continue

        view_url = _absolute(base_url, view_href)
        pdf_url = _absolute(base_url, pdf_href)
        document_uuid, collection_uuid = _uuids(view_url)

        rows.append(
            ListRow(
                page=page,
                title=_cell_text(cells[1]),
                court=_cell_text(cells[3]),
                judge=_cell_text(cells[4]),
                date_delivered=_safe_date(_cell_text(cells[0])),
                date_uploaded=_safe_date(_cell_text(cells[5])),
                view_url=view_url,
                pdf_url=pdf_url,
                collection_uuid=collection_uuid,
                document_uuid=document_uuid,
            )
        )
    return rows


def _safe_date(raw: str) -> str | None:
    """Parse a date, degrading to ``None`` instead of aborting on odd input."""
    try:
        return parse_irish_date(raw)
    except ValueError:
        return None


def parse_result_count(html: str) -> int | None:
    """Return the total result count advertised on the page, if present."""
    match = _COUNT_RE.search(_soup(html).get_text(" ", strip=True))
    return int(match.group(1).replace(",", "")) if match else None


def parse_last_page(html: str) -> int:
    """Return the highest zero-based page index reachable from the pager.

    Returns ``0`` when no pager links are present (a single-page result set).
    """
    pager = _soup(html).find("div", class_="search-pager")
    if not isinstance(pager, Tag):
        return 0

    pages = [
        int(m.group(1))
        for a in pager.find_all("a", href=True)
        if (m := _PAGE_RE.search(a["href"]))
    ]
    return max(pages) if pages else 0
