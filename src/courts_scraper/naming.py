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
from pathlib import Path

# A neutral citation looks like: [2026] IESC 36 / [2019] IECA 112 / [2020] IEHC 5.
# Matched case-insensitively: older judgments carry a lower-cased court token
# (e.g. ``[2015] IEHc 168``, ``[2009] iehc 537``); the code is normalised to
# upper case in the slug so those name canonically alongside their peers.
_CITATION_RE = re.compile(
    r"^\[(?P<year>\d{4})\]\s+(?P<code>IE[A-Za-z]+)\s+(?P<num>\d+)", re.IGNORECASE
)

# Characters we allow in a slug; everything else collapses to the separator.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9]+")

# Path separators (both conventions) -- a filename must contain neither.
_PATH_SEPARATORS = re.compile(r"[/\\]")

# Cap the judge portion so a pathological cell value cannot exceed the OS
# filename length limit (typically 255 bytes) once combined with the citation.
_MAX_JUDGE_SLUG = 80


class UnsafeFilenameError(ValueError):
    """Raised when a filename is not a plain, in-directory component.

    Defends the download step against a filename that would escape the output
    directory (path traversal), e.g. from a tampered database.
    """


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

    return f"{match['year']}_{match['code'].upper()}_{match['num']}"


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


def safe_output_path(directory: Path, filename: str) -> Path:
    """Join ``filename`` onto ``directory``, rejecting anything that escapes it.

    ``filename`` must be a plain component (no path separators, not ``.``/``..``,
    not hidden, not absolute) that resolves to a direct child of ``directory``.
    This is a defence-in-depth check at the filesystem write boundary: filenames
    produced by :func:`pdf_filename` are already safe, but a tampered database
    row must never be able to write outside the run's PDF folder.

    Raises:
        UnsafeFilenameError: If ``filename`` is not a safe in-directory name.
    """
    if (
        not filename
        or filename in {".", ".."}
        or filename.startswith(".")
        or _PATH_SEPARATORS.search(filename)
    ):
        raise UnsafeFilenameError(f"unsafe filename: {filename!r}")

    resolved = (directory / filename).resolve()
    if resolved.parent != directory.resolve():
        raise UnsafeFilenameError(
            f"filename escapes the output directory: {filename!r}"
        )
    return resolved
