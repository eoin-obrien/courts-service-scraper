"""Construction of courts.ie search URLs.

This module is the single extensibility point for widening the crawl. The site
encodes its faceted search as an Alfresco query embedded in the URL *path*
(not the query string), for example::

    /search/Judgments/" type:Judgment" AND "filter:alfresco_radio.title"
        AND "filter:alfresco_Court.Supreme Court"

(shown decoded). Adding another court is a one-line change: extend the
:class:`Court` enum. No other module needs to change.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from urllib.parse import quote


class Court(StrEnum):
    """Courts selectable via the ``filter:alfresco_Court.<name>`` facet.

    The value is the court name exactly as the site expects it in the filter.
    The name (left-hand side) is the short token accepted on the command line.
    """

    SUPREME = "Supreme Court"
    COURT_OF_APPEAL = "Court of Appeal"
    HIGH = "High Court"

    @classmethod
    def from_token(cls, token: str) -> Court:
        """Resolve a CLI token (e.g. ``"supreme"``) to a :class:`Court`.

        Args:
            token: Case-insensitive short name; underscores or hyphens accepted
                in place of spaces (``"court-of-appeal"``, ``"court_of_appeal"``).

        Raises:
            ValueError: If the token does not match a known court.
        """
        key = token.strip().upper().replace("-", "_").replace(" ", "_")
        try:
            return cls[key]
        except KeyError:
            valid = ", ".join(c.name.lower() for c in cls)
            raise ValueError(
                f"unknown court {token!r}; choose one of: {valid}"
            ) from None


def build_query(courts: Iterable[Court], *, doc_type: str = "Judgment") -> str:
    """Build the (decoded) Alfresco query string for the given courts.

    Args:
        courts: Courts to include. Multiple courts are combined with ``OR``.
        doc_type: Alfresco document type; defaults to ``"Judgment"``.

    Returns:
        The decoded query string, e.g.
        ``'" type:Judgment" AND "filter:alfresco_radio.title" AND'
        ' "filter:alfresco_Court.Supreme Court"'``.

    Raises:
        ValueError: If ``courts`` is empty.

    Note:
        The single-court form is verified against the live site. The
        multi-court ``OR`` grouping is constructed by analogy and should be
        confirmed against live results before relying on it.
    """
    court_list = list(courts)
    if not court_list:
        raise ValueError("at least one court is required")

    # Leading space inside the first quote is intentional -- it mirrors the
    # exact token the site emits and must be preserved for a byte-identical URL.
    parts = [f'" type:{doc_type}"', '"filter:alfresco_radio.title"']

    court_filters = [f'"filter:alfresco_Court.{c.value}"' for c in court_list]
    if len(court_filters) == 1:
        parts.append(court_filters[0])
    else:
        parts.append("(" + " OR ".join(court_filters) + ")")

    return " AND ".join(parts)


def search_url(base_url: str, query: str, *, page: int = 0) -> str:
    """Return the absolute search URL for a query and page.

    Args:
        base_url: Site origin, e.g. ``"https://ww2.courts.ie"``.
        query: Decoded query string from :func:`build_query`.
        page: Zero-based page number.

    Returns:
        The absolute, percent-encoded URL for that search page.
    """
    # ``safe=""`` encodes spaces as %20, quotes as %22 and colons as %3A while
    # leaving unreserved characters (``. _ - ~``) intact -- matching the site.
    encoded = quote(query, safe="")
    return f"{base_url.rstrip('/')}/search/Judgments/{encoded}?page={page}"
