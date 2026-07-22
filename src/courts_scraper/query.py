"""Construction of courts.ie search URLs.

This module is the single extensibility point for widening the crawl. The site
encodes its faceted search as an Alfresco query embedded in the URL *path*
(not the query string), for example::

    /search/Judgments/" type:Judgment" AND "filter:alfresco_radio.title"
        AND "filter:alfresco_Court.Supreme Court"

(shown decoded). Adding another court is a one-line change: extend the
:class:`Court` enum. No other module needs to change.

Selecting several courts at once repeats the ``filter:alfresco_Court.<name>``
token, each joined by ``AND``. The site's Alfresco parser treats repeated
values of the *same* facet as a union, so this yields the OR of those courts
(verified live: Supreme + High returns 16,437 results, exactly the two courts).
An explicit ``(A OR B)`` grouping does **not** work -- the parser drops the
whole court constraint and returns the entire unfiltered corpus.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from urllib.parse import quote


class Court(StrEnum):
    """Courts selectable via the ``filter:alfresco_Court.<name>`` facet.

    The value is the court name exactly as the site expects it in the filter
    (verified against the live "Jurisdiction" facet on the judgments search).
    The name (left-hand side) is the short token accepted on the command line.
    """

    SUPREME = "Supreme Court"
    COURT_OF_APPEAL = "Court of Appeal"
    HIGH = "High Court"
    COURT_OF_CRIMINAL_APPEAL = "Court of Criminal Appeal"
    COURTS_MARTIAL_APPEAL = "Courts-Martial Appeal Courts"
    CENTRAL_CRIMINAL = "Central Criminal Court"
    SPECIAL_CRIMINAL = "Special Criminal Court"
    CIRCUIT = "Circuit Court"
    DISTRICT = "District Court"

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


# Named groups that expand to several courts, accepted anywhere a court token
# is. ``superior`` is the constitutional Superior Courts of Ireland; ``all``
# is every selectable facet.
COURT_GROUPS: dict[str, tuple[Court, ...]] = {
    "superior": (Court.SUPREME, Court.COURT_OF_APPEAL, Court.HIGH),
    "all": tuple(Court),
}


def resolve_court_tokens(tokens: Iterable[str]) -> tuple[Court, ...]:
    """Resolve CLI tokens -- individual courts or group aliases -- to courts.

    Group aliases (see :data:`COURT_GROUPS`, e.g. ``"superior"``) expand to
    several courts. Duplicates are removed while preserving first-seen order,
    so ``--court superior --court high`` is the same three courts, once each.

    Args:
        tokens: CLI tokens, each a court short name or a group alias.

    Returns:
        The resolved courts, deduplicated, in first-seen order.

    Raises:
        ValueError: If a token is neither a known court nor a group alias.
    """
    resolved: list[Court] = []
    for token in tokens:
        group = COURT_GROUPS.get(token.strip().lower())
        courts = group if group is not None else (Court.from_token(token),)
        for court in courts:
            if court not in resolved:
                resolved.append(court)
    return tuple(resolved)


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
        Multiple courts each contribute a ``filter:alfresco_Court.<name>``
        token joined by ``AND``. The site unions repeated values of the same
        facet, so the result is the OR of the courts. Verified live: Supreme +
        High returns 16,437 results (the two courts, nothing else).
    """
    court_list = list(courts)
    if not court_list:
        raise ValueError("at least one court is required")

    # Leading space inside the first quote is intentional -- it mirrors the
    # exact token the site emits and must be preserved for a byte-identical URL.
    parts = [f'" type:{doc_type}"', '"filter:alfresco_radio.title"']
    parts.extend(f'"filter:alfresco_Court.{c.value}"' for c in court_list)

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
