"""Export a run to a Frictionless Data Package (CSV + JSON + optional Parquet).

One derivation pass (via :mod:`courts_scraper.dataset`) feeds every output
encoding, so the formats cannot drift from one another. :data:`EXPORT_FIELDS` is
the single source of truth for column order, types, and descriptions -- it drives
the CSV header, the ``datapackage.json`` Table Schema, and the generated data
dictionary alike.

Design decisions encoded here (see the eng-review design doc):

* ``primaryKey`` is ``document_uuid`` (stable per opinion). Neutral citation is
  emitted but is **case-level and non-unique**, never a key.
* The descriptor is hand-rolled (no ``frictionless`` runtime dependency); its
  shape follows the Data Package / Table Schema specs.
* Parquet needs ``pyarrow`` (an optional extra); requesting it without the
  dependency raises a clear :class:`ExportError`, never a bare ``ImportError``.
* Rights are a positive public-domain statement (judicial judgments), with the
  data-protection / reporting-restriction caveat carried as a note.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from courts_scraper import __version__
from courts_scraper.dataset import DERIVE_VERSION, Derived, RowLike, iter_records
from courts_scraper.db import SCHEMA_VERSION

_PANEL_JOIN = ";"
_FLAG_JOIN = " | "

_RIGHTS_NOTE = (
    "Judicial judgments of the Courts Service of Ireland are public domain and "
    "free to reproduce. This does not remove data-protection obligations or "
    "reporting restrictions that may apply to personal data within a judgment; "
    "verify before reprocessing or onward publication."
)


class ExportError(Exception):
    """Raised for an export that cannot be produced (e.g. Parquet without pyarrow)."""


@dataclass(frozen=True, slots=True)
class Field:
    """One export column: its Table Schema type plus human-facing documentation."""

    name: str
    type: str
    title: str
    description: str
    format: str | None = None


# The data dictionary. Order here is the CSV column order and the Table Schema
# field order. Every consumer-facing column is documented once, here.
EXPORT_FIELDS: tuple[Field, ...] = (
    Field(
        "document_uuid",
        "string",
        "Document ID",
        "Stable Alfresco id for this specific document/opinion. Primary key.",
    ),
    Field(
        "collection_uuid",
        "string",
        "Collection ID",
        "Alfresco id grouping all documents of one judgment/case.",
    ),
    Field(
        "neutral_citation",
        "string",
        "Neutral citation",
        "e.g. '[2026] IESC 36'. Case-level: repeats across a case's opinions, "
        "so NOT a row identifier.",
    ),
    Field("title", "string", "Case title", "Case title as published."),
    Field("court", "string", "Court", "Court name as published."),
    Field(
        "authoring_judge",
        "string",
        "Authoring judge",
        "Author of THIS opinion (not the whole bench).",
    ),
    Field(
        "panel",
        "string",
        "Panel",
        f"Full bench that heard the case, '{_PANEL_JOIN}'-separated. See panel_count.",
    ),
    Field("panel_count", "integer", "Panel size", "Number of judges on the panel."),
    Field(
        "date_delivered",
        "date",
        "Date delivered",
        "Delivery date (ISO 8601).",
        "%Y-%m-%d",
    ),
    Field(
        "date_uploaded", "date", "Date uploaded", "Upload date (ISO 8601).", "%Y-%m-%d"
    ),
    Field("record_number", "string", "Record number", "Court record number."),
    Field("status", "string", "Status", "Status label as served (e.g. 'Approved')."),
    Field(
        "status_in_vocab",
        "boolean",
        "Status in vocabulary",
        "False flags a status value outside the observed controlled vocabulary.",
    ),
    Field(
        "result", "string", "Result", "Result label as served (e.g. 'Allow Appeal')."
    ),
    Field(
        "result_in_vocab",
        "boolean",
        "Result in vocabulary",
        "False flags a result value outside the observed controlled vocabulary.",
    ),
    Field(
        "vocab_flags",
        "string",
        "Vocabulary flags",
        "Human-readable drift warnings, empty when all values are in vocabulary.",
    ),
    Field("view_url", "string", "View URL", "Judgment view page.", "uri"),
    Field("pdf_url", "string", "PDF URL", "Direct PDF download URL.", "uri"),
    Field("filename", "string", "Filename", "Local PDF filename in the bundle."),
    Field("sha256", "string", "SHA-256", "Hex SHA-256 of the downloaded PDF."),
    Field("bytes", "integer", "Bytes", "On-disk size of the downloaded PDF."),
    Field(
        "http_content_type",
        "string",
        "HTTP Content-Type",
        "Content-Type header served with the PDF.",
    ),
    Field(
        "http_content_length",
        "integer",
        "HTTP Content-Length",
        "Content-Length header served with the PDF (often absent).",
    ),
    Field(
        "http_last_modified",
        "string",
        "HTTP Last-Modified",
        "Last-Modified header served with the PDF (often absent).",
    ),
    Field("http_etag", "string", "HTTP ETag", "ETag header served with the PDF."),
    Field(
        "listed_at",
        "datetime",
        "Listed at",
        "When the search row was first seen (UTC ISO 8601).",
    ),
    Field(
        "meta_retrieved_at",
        "datetime",
        "Metadata retrieved at",
        "When the view page was scraped (UTC ISO 8601).",
    ),
    Field(
        "pdf_retrieved_at",
        "datetime",
        "PDF retrieved at",
        "When the PDF was fetched and verified (UTC ISO 8601).",
    ),
    Field("meta_status", "string", "Metadata status", "pending | ok | error."),
    Field("download_status", "string", "Download status", "pending | done | error."),
)

_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in EXPORT_FIELDS)


@dataclass(frozen=True, slots=True)
class ExportResult:
    """What an export produced: the files written and the row count."""

    files: tuple[Path, ...]
    record_count: int


def data_dictionary_markdown() -> str:
    """Render :data:`EXPORT_FIELDS` as a human-readable data dictionary.

    The Table Schema and this document share one source, so they can never
    disagree. Written into every export/corpus bundle and mirrored (generated) at
    ``docs/DATA_DICTIONARY.md`` in the repo.
    """
    lines = [
        "# Data dictionary",
        "",
        "Generated from the export Table Schema (`EXPORT_FIELDS`). Do not edit by "
        "hand; regenerate with `courts-scraper data-dictionary`.",
        "",
        "Primary key: `document_uuid`. Missing values are the empty string.",
        "",
        "| Column | Type | Format | Description |",
        "|--------|------|--------|-------------|",
    ]
    for f in EXPORT_FIELDS:
        desc = f.description.replace("|", "\\|")
        lines.append(f"| `{f.name}` | {f.type} | {f.format or ''} | {desc} |")
    return "\n".join(lines) + "\n"


def _flat_row(raw: RowLike, derived: Derived) -> dict[str, object]:
    """Build the flat (tabular) row: passthrough columns + derived columns.

    Keys are exactly :data:`_FIELD_NAMES` (guarded by a test). Values are kept as
    native Python objects; encoding to CSV/Parquet happens at write time.
    """
    return {
        "document_uuid": raw["document_uuid"],
        "collection_uuid": raw["collection_uuid"],
        "neutral_citation": raw["neutral_citation"],
        "title": raw["title"],
        "court": raw["court"],
        "authoring_judge": derived.authoring_judge,
        "panel": _PANEL_JOIN.join(derived.panel),
        "panel_count": len(derived.panel),
        "date_delivered": raw["date_delivered"],
        "date_uploaded": raw["date_uploaded"],
        "record_number": raw["record_number"],
        "status": derived.status,
        "status_in_vocab": derived.status_in_vocab,
        "result": derived.result,
        "result_in_vocab": derived.result_in_vocab,
        "vocab_flags": _FLAG_JOIN.join(derived.flags),
        "view_url": raw["view_url"],
        "pdf_url": raw["pdf_url"],
        "filename": raw["filename"],
        "sha256": raw["sha256"],
        "bytes": raw["bytes"],
        "http_content_type": raw["http_content_type"],
        "http_content_length": raw["http_content_length"],
        "http_last_modified": raw["http_last_modified"],
        "http_etag": raw["http_etag"],
        "listed_at": raw["listed_at"],
        "meta_retrieved_at": raw["meta_retrieved_at"],
        "pdf_retrieved_at": raw["pdf_retrieved_at"],
        "meta_status": raw["meta_status"],
        "download_status": raw["download_status"],
    }


def _json_record(raw: RowLike, derived: Derived) -> dict[str, object]:
    """A JSON record: the flat row plus nested fields flat CSV cannot carry."""
    record = _flat_row(raw, derived)
    record["panel"] = list(derived.panel)  # array, not the ';'-joined string
    meta_raw = raw["meta_json"]
    if meta_raw:
        parsed = json.loads(str(meta_raw))
        record["metadata_fields"] = parsed.get("fields")
        record["supplementary"] = parsed.get("supplementary")
    return record


def _csv_value(value: object) -> str:
    """Encode one cell for CSV: None -> '' (missingValues), bool -> true/false."""
    if value is None:
        return ""
    if isinstance(value, bool):  # bool before int -- bool is an int subclass
        return "true" if value else "false"
    return str(value)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _schema() -> dict[str, object]:
    fields: list[dict[str, object]] = []
    for f in EXPORT_FIELDS:
        entry: dict[str, object] = {
            "name": f.name,
            "type": f.type,
            "title": f.title,
            "description": f.description,
        }
        if f.format is not None:
            entry["format"] = f.format
        fields.append(entry)
    return {"fields": fields, "primaryKey": "document_uuid", "missingValues": [""]}


def _coverage(rows: list[dict[str, object]]) -> dict[str, object]:
    dates = sorted(
        str(r["date_delivered"]) for r in rows if r["date_delivered"] is not None
    )
    return {
        "record_count": len(rows),
        "date_delivered_min": dates[0] if dates else None,
        "date_delivered_max": dates[-1] if dates else None,
    }


def _manifest(run_dir: Path) -> dict[str, object]:
    path = run_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def build_datapackage(
    rows: list[dict[str, object]],
    *,
    base_url: str | None = None,
    courts: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the ``datapackage.json`` descriptor for the exported rows.

    ``base_url``/``courts`` come from the run manifest (single-run export) or the
    merge inputs (corpus). ``extra`` merges in extra top-level keys, e.g. a corpus
    snapshot's source-run set.
    """
    sources = []
    if base_url:
        sources.append({"title": "Courts Service of Ireland", "path": base_url})
    descriptor: dict[str, object] = {
        "name": "courts-scraper-judgments",
        "title": "Courts Service of Ireland -- written judgments",
        "profile": "tabular-data-package",
        "created": _now_iso(),
        "tool": {"name": "courts-scraper", "version": __version__},
        "schema_version": SCHEMA_VERSION,
        "derive_version": DERIVE_VERSION,
        "courts": courts or [],
        "coverage": _coverage(rows),
        "licenses": [
            {
                "title": "Public domain -- judicial judgments of Ireland",
                "path": base_url or "https://ww2.courts.ie",
            }
        ],
        "rights_note": _RIGHTS_NOTE,
        "sources": sources,
        "resources": [
            {
                "name": "judgments",
                "path": "judgments.csv",
                "profile": "tabular-data-resource",
                "format": "csv",
                "mediatype": "text/csv",
                "encoding": "utf-8",
                "schema": _schema(),
            }
        ],
    }
    if extra:
        descriptor.update(extra)
    return descriptor


def _write_csv(rows: list[dict[str, object]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_FIELD_NAMES)
        for row in rows:
            writer.writerow([_csv_value(row[name]) for name in _FIELD_NAMES])


def _write_json(records: list[dict[str, object]], path: Path) -> None:
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_parquet(rows: list[dict[str, object]], path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # optional extra not installed
        raise ExportError(
            "Parquet export needs pyarrow. Install it with: "
            "pip install 'courts-scraper[parquet]'"
        ) from exc

    columns = {name: [row[name] for row in rows] for name in _FIELD_NAMES}
    pq.write_table(pa.table(columns), path)


_WRITERS = {
    "csv": "judgments.csv",
    "json": "judgments.json",
    "parquet": "judgments.parquet",
}


def write_package(
    pairs: Iterable[tuple[RowLike, Derived]],
    out_dir: Path,
    *,
    formats: Iterable[str] = ("csv", "json"),
    base_url: str | None = None,
    courts: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> ExportResult:
    """Write a Data Package into ``out_dir`` from ``(raw, derived)`` pairs.

    The reusable core shared by single-run export and corpus assembly. A single
    pass over ``pairs`` feeds every requested format, so they cannot drift.

    Raises:
        ExportError: For an unknown format, or Parquet without ``pyarrow``.
    """
    requested = list(dict.fromkeys(formats))  # de-dupe, preserve order
    unknown = [f for f in requested if f not in _WRITERS]
    if unknown:
        raise ExportError(f"unknown export format(s): {', '.join(unknown)}")

    flat: list[dict[str, object]] = []
    nested: list[dict[str, object]] = []
    for raw, derived in pairs:
        flat.append(_flat_row(raw, derived))
        if "json" in requested:
            nested.append(_json_record(raw, derived))

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if "csv" in requested:
        path = out_dir / _WRITERS["csv"]
        _write_csv(flat, path)
        written.append(path)
    if "json" in requested:
        path = out_dir / _WRITERS["json"]
        _write_json(nested, path)
        written.append(path)
    if "parquet" in requested:
        path = out_dir / _WRITERS["parquet"]
        _write_parquet(flat, path)  # may raise ExportError before we claim success
        written.append(path)

    descriptor = out_dir / "datapackage.json"
    descriptor.write_text(
        json.dumps(
            build_datapackage(flat, base_url=base_url, courts=courts, extra=extra),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    written.append(descriptor)

    dictionary = out_dir / "DATA_DICTIONARY.md"
    dictionary.write_text(data_dictionary_markdown(), encoding="utf-8")
    written.append(dictionary)

    return ExportResult(files=tuple(written), record_count=len(flat))


def export_run(
    run_dir: Path, out_dir: Path, formats: Iterable[str] = ("csv", "json")
) -> ExportResult:
    """Export a single ``run_dir`` to a Data Package in ``out_dir``.

    Reads the run's manifest for source/court metadata, then delegates to
    :func:`write_package`.

    Raises:
        ExportError: For an unknown format, or Parquet without ``pyarrow``.
    """
    manifest = _manifest(run_dir)
    base_url = manifest.get("base_url")
    courts = manifest.get("courts")
    return write_package(
        iter_records(run_dir),
        out_dir,
        formats=formats,
        base_url=base_url if isinstance(base_url, str) else None,
        courts=courts if isinstance(courts, list) else None,
    )
