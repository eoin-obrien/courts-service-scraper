"""Filename construction from a judgment's Neutral Citation and judge.

Every judgment on the site carries a Neutral Citation (e.g. ``[2026] IESC 36``)
and is the mandatory basis for a file's identity. Because one citation can map
to several opinions (a concurring and a dissenting judgment, say), the authoring
judge is appended to keep filenames unique and human-meaningful::

    [2026] IESC 36 + "Woulfe J."  ->  2026_IESC_36_Woulfe-J.pdf
    [2026] IESC 36 + "Hogan J."   ->  2026_IESC_36_Hogan-J.pdf
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Container

# A neutral citation looks like: [2026] IESC 36 / [2019] IECA 112 / [2020] IEHC 5
_CITATION_RE = re.compile(r"^\[(?P<year>\d{4})\]\s+(?P<code>IE[A-Z]+)\s+(?P<num>\d+)")

# Characters we allow in a slug; everything else collapses to the separator.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9]+")

# Cap the judge portion so a pathological cell value cannot exceed the OS
# filename length limit (typically 255 bytes) once combined with the citation.
_MAX_JUDGE_SLUG = 80


class MissingCitationError(ValueError):
    """Raised when a judgment has no usable Neutral Citation.

    Neutral Citation is mandatory for filing; a judgment without one cannot be
    named and is recorded as an error for manual follow-up.
    """


def citation_slug(citation: str | None) -> str:
    """Convert a Neutral Citation into a filesystem-safe slug.

    Args:
        citation: e.g. ``"[2026] IESC 36"``.

    Returns:
        e.g. ``"2026_IESC_36"``.

    Raises:
        MissingCitationError: If ``citation`` is empty or not a recognisable
            neutral citation.
    """
    text = (citation or "").strip()
    if not text:
        raise MissingCitationError("no neutral citation present")

    match = _CITATION_RE.match(text)
    if not match:
        raise MissingCitationError(f"unrecognised neutral citation: {citation!r}")

    return f"{match['year']}_{match['code']}_{match['num']}"


def judge_slug(judge: str | None) -> str:
    """Convert a judge label into a filesystem-safe slug.

    Accented characters common in Irish names are transliterated to ASCII
    (``"Ó Caoimh J."`` -> ``"O-Caoimh-J"``) rather than dropped, so distinct
    judges do not collapse to the same slug. The result is length-capped so a
    pathological value cannot blow past the filesystem's filename limit.

    Args:
        judge: e.g. ``"Woulfe J."``. May be empty.

    Returns:
        e.g. ``"Woulfe-J"``; an empty label yields an empty string.
    """
    # NFKD splits accented letters into base + combining mark; dropping the
    # non-ASCII bytes then keeps the base letter (é -> e, Ó -> O).
    decomposed = unicodedata.normalize("NFKD", (judge or "").strip())
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = _SAFE_CHARS.sub("-", ascii_text).strip("-")
    return slug[:_MAX_JUDGE_SLUG].rstrip("-")


def pdf_filename(
    citation: str | None,
    judge: str | None,
    *,
    taken: Container[str] = (),
) -> str:
    """Build a unique ``.pdf`` filename for a judgment document.

    Args:
        citation: The Neutral Citation (mandatory).
        judge: The authoring judge label (optional but recommended).
        taken: Filenames already in use; a numeric suffix is added on collision.

    Returns:
        A unique filename such as ``"2026_IESC_36_Woulfe-J.pdf"``.

    Raises:
        MissingCitationError: If the citation is missing or unrecognisable.
    """
    stem = citation_slug(citation)
    judge_part = judge_slug(judge)
    if judge_part:
        stem = f"{stem}_{judge_part}"

    candidate = f"{stem}.pdf"
    if candidate not in taken:
        return candidate

    # Deterministic collision suffix: _2, _3, ...
    index = 2
    while f"{stem}_{index}.pdf" in taken:
        index += 1
    return f"{stem}_{index}.pdf"
