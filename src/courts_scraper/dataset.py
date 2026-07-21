"""Derivation layer: raw ``record`` rows -> research-facing derived fields.

This is the single place that turns captured truth into the interoperable,
labelled fields a dataset consumer wants. Both ``export`` and ``corpus`` read
through here, so the rules live once and are fixed once.

Design decisions this module encodes (see the eng-review design doc):

* **Derive at export.** Nothing here is written back into ``record``. A wrong
  rule is fixed by re-running a derivation, never by re-scraping.
* **Author vs panel are different things.** ``record.judge`` is the author of
  *this* opinion; ``composition`` is the whole bench. They are kept as two
  separate derived fields (:attr:`Derived.authoring_judge` and
  :attr:`Derived.panel`) and never merged.
* **Controlled vocabularies warn, never reject.** An out-of-vocab status/result
  is emitted as-is and flagged, so government label drift surfaces instead of
  silently corrupting an enum or aborting a crawl.
* **ECLI is deliberately not derived yet.** Ireland has no confirmed public ECLI
  court-code scheme, so emitting one would be fabrication. Derive-at-export means
  it can be added later with a re-export and no re-scrape.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from courts_scraper.db import RECORD_COLUMNS, open_readonly


class RowLike(Protocol):
    """The minimal read interface :func:`derive` needs.

    Satisfied structurally by both :class:`sqlite3.Row` and a plain ``dict``, so
    derivation is a pure function testable with dict fixtures.
    """

    def __getitem__(self, key: str) -> object: ...


# Bumped whenever a derivation rule changes. Stamped into a published snapshot so
# a DOI pins one interpretation of the raw data, not a moving target.
DERIVE_VERSION = 1

# Empirically-observed controlled vocabularies. Seeded from real scraped values;
# anything outside is flagged as drift (not rejected) so new labels get noticed
# and codified here rather than silently accepted. Extend as the corpus surfaces
# more values -- do not guess entries that have not been observed.
STATUS_VOCAB: frozenset[str] = frozenset({"Approved"})
RESULT_VOCAB: frozenset[str] = frozenset({"Allow Appeal"})

_PANEL_DELIMITER = ";"


@dataclass(frozen=True, slots=True)
class Derived:
    """Fields derived from one raw ``record`` row, for export/corpus.

    Attributes:
        authoring_judge: Author of *this* opinion (from ``record.judge``); ``""``
            when the source left it blank.
        panel: The full bench that heard the case (from ``composition``), split on
            ``;``. Empty when composition is absent.
        status: ``Status`` value as served (or ``None``).
        status_in_vocab: Whether ``status`` is a known value. ``True`` when status
            is ``None`` (absence is not drift).
        result: ``Result`` value as served (or ``None``).
        result_in_vocab: Whether ``result`` is a known value. ``True`` when result
            is ``None``.
        flags: Human-readable drift warnings, empty when everything is in vocab.
    """

    authoring_judge: str
    panel: tuple[str, ...]
    status: str | None
    status_in_vocab: bool
    result: str | None
    result_in_vocab: bool
    flags: tuple[str, ...]


def _split_panel(composition: str | None) -> tuple[str, ...]:
    """Split a semicolon-delimited composition into trimmed judge names.

    Handles the real-world messiness: ``None``/empty -> ``()``; a single name ->
    one element; trailing or doubled delimiters -> no empty entries.
    """
    if not composition:
        return ()
    parts = (name.strip() for name in composition.split(_PANEL_DELIMITER))
    return tuple(name for name in parts if name)


def _check_vocab(
    value: str | None, vocab: frozenset[str], label: str
) -> tuple[bool, str | None]:
    """Return ``(in_vocab, flag)`` for ``value`` against ``vocab``.

    A ``None`` value (field absent) is in-vocab with no flag -- absence is not
    drift. A present, unknown value is out-of-vocab and produces a flag string.
    """
    if value is None or value in vocab:
        return True, None
    return False, f"{label} not in controlled vocabulary: {value!r}"


def derive(row: RowLike) -> Derived:
    """Compute derived fields for one raw ``record`` row.

    Pure function of the row, so it is trivially testable and re-runnable. Accepts
    any mapping (a :class:`sqlite3.Row` or a plain dict in tests).
    """
    authoring_judge = str(row["judge"] or "")
    panel = _split_panel(_as_str(row["composition"]))

    status = _as_str(row["status_field"])
    result = _as_str(row["result"])
    status_ok, status_flag = _check_vocab(status, STATUS_VOCAB, "status")
    result_ok, result_flag = _check_vocab(result, RESULT_VOCAB, "result")
    flags = tuple(f for f in (status_flag, result_flag) if f is not None)

    return Derived(
        authoring_judge=authoring_judge,
        panel=panel,
        status=status,
        status_in_vocab=status_ok,
        result=result,
        result_in_vocab=result_ok,
        flags=flags,
    )


def _as_str(value: object) -> str | None:
    """Coerce a nullable DB cell to ``str | None`` (empty string -> ``None``)."""
    if value is None:
        return None
    text = str(value)
    return text or None


def iter_records(run_dir: Path) -> Iterator[tuple[dict[str, object], Derived]]:
    """Open a run's DB (migrating on open) and yield ``(raw_row, derived)`` pairs.

    The raw row is materialised to a plain dict so it outlives the DB connection
    (the corpus merge holds rows across several runs). The derived object is the
    projection. Both are handed to consumers so an exporter writes passthrough and
    derived columns from a single pass.
    """
    conn = open_readonly(run_dir / "judgments.sqlite")
    try:
        for row in conn.execute("SELECT * FROM record ORDER BY id"):
            # Full expected shape, None-filled, then overlay the columns this DB
            # actually has -- so an older run reads back with NULL provenance and
            # no migration (the archive is never mutated).
            raw: dict[str, object] = dict.fromkeys(RECORD_COLUMNS)
            raw.update(zip(row.keys(), row, strict=True))
            yield raw, derive(raw)
    finally:
        conn.close()
