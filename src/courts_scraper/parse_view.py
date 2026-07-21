"""Parsing of an individual judgment's view page.

Metadata is laid out as a grid of cells, each a ``div`` containing a
``span.cell-title`` label followed by its value, for example a cell whose
``span.cell-title`` reads ``Neutral Citation`` followed by ``[2026] IESC 36``.

We capture every label/value pair verbatim (archived as JSON for research) and
pull out the Neutral Citation specifically, since it is mandatory and drives
file naming. Links to related documents (signed copies, memos, summaries, Word
versions) are recorded for provenance but not downloaded.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag

from courts_scraper.models import JudgmentMeta, SupplementaryDoc

_NEUTRAL_CITATION_LABEL = "Neutral Citation"


def parse_view_page(html: str, base_url: str) -> JudgmentMeta:
    """Parse a judgment view page into :class:`JudgmentMeta`.

    Args:
        html: Raw HTML of the view page.
        base_url: Site origin used to absolutise related-document links.

    Returns:
        The scraped metadata. ``neutral_citation`` is ``None`` when the page
        carries no such field; callers treat that as an error condition.
    """
    soup = BeautifulSoup(html, "lxml")

    fields = _extract_fields(soup)
    supplementary = _extract_supplementary(soup, base_url)
    citation = fields.get(_NEUTRAL_CITATION_LABEL) or None

    return JudgmentMeta(
        neutral_citation=citation,
        fields=fields,
        supplementary=supplementary,
    )


def _extract_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Return the label -> value map from every ``span.cell-title`` cell."""
    fields: dict[str, str] = {}
    for title in soup.find_all("span", class_="cell-title"):
        if not isinstance(title, Tag):
            continue
        cell = title.parent
        if not isinstance(cell, Tag):
            continue

        label = title.get_text(" ", strip=True)
        full = cell.get_text(" ", strip=True)
        value = full[len(label) :].strip(" :") if full.startswith(label) else full
        if label:
            fields[label] = value
    return fields


def _extract_supplementary(
    soup: BeautifulSoup, base_url: str
) -> list[SupplementaryDoc]:
    """Collect links to Alfresco documents (the judgment's full file bundle)."""
    seen: set[str] = set()
    docs: list[SupplementaryDoc] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        if not isinstance(href, str) or "/acc/alfresco/" not in href:
            continue

        url = _absolute(base_url, href)
        if url in seen:
            continue
        seen.add(url)

        label = anchor.get_text(" ", strip=True) or url.rsplit("/", 2)[-2]
        docs.append(SupplementaryDoc(label=label, url=url))
    return docs


def _absolute(base_url: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, href.split("#", 1)[0]).replace(" ", "%20")
