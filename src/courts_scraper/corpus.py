"""Merge runs into one citable, fixity-checked corpus bundle (a BagIt bag).

Turns a pile of timestamped runs into a single publishable artifact:

* **Deduplicate by ``document_uuid``** (the stable Alfresco id), keeping the most
  recently fetched version (``pdf_retrieved_at``, then metadata/listing time).
* **Surface content mutations, never hide them.** If one ``document_uuid`` was
  served with more than one distinct ``sha256`` across runs, that is a reported
  :class:`Conflict` -- latest still wins for the row, but the divergence is
  recorded, not silently dropped.
* **BagIt fixity over the actual payload.** Every packaged file (CSV/JSON/
  descriptor/PDFs/docs) is hashed fresh into ``manifest-sha256.txt``; the stored
  per-PDF ``sha256`` is only a pre-bag input check.
* **A frozen, versioned snapshot.** ``snapshot.json`` stamps schema_version,
  derive_version, tool version, and the exact source-run set, so a DOI minted
  over the bag pins one immutable interpretation of the data.
* **Discovery + documentation.** A schema.org ``dataset.jsonld`` and a
  ``DATASHEET.md`` (Gebru et al.) travel inside the bag.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from courts_scraper import __version__
from courts_scraper.dataset import DERIVE_VERSION, Derived, iter_records
from courts_scraper.db import SCHEMA_VERSION, read_pdf_versions
from courts_scraper.download import sha256_of
from courts_scraper.export import EXPORT_FIELDS, ExportError, write_package
from courts_scraper.naming import UnsafeFilenameError, safe_output_path

_BAGIT_VERSION = "1.0"
_DL_DONE = "done"
# Cap on revision entries embedded verbatim in snapshot.json, so a corpus with a
# very long revision history cannot bloat (or, from a hostile run, OOM) the bag.
# The full count is always reported; only the embedded list is truncated.
_MAX_SNAPSHOT_REVISIONS = 1000


@dataclass(frozen=True, slots=True)
class Conflict:
    """One ``document_uuid`` served with differing content across runs."""

    document_uuid: str
    sha256s: tuple[str, ...]
    runs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Outcome of merging several runs into a deduplicated record set."""

    pairs: list[tuple[dict[str, object], Derived]]
    source_run: dict[str, str]  # record identity -> winning run name
    conflicts: list[Conflict]
    run_names: list[str]


@dataclass(frozen=True, slots=True)
class CorpusResult:
    """What building a corpus produced."""

    bag_dir: Path
    record_count: int
    conflicts: tuple[Conflict, ...]
    missing_pdfs: tuple[str, ...]


def _identity(raw: dict[str, object]) -> str:
    """Stable per-document identity: ``document_uuid``, or ``pdf_url`` if blank."""
    return str(raw["document_uuid"] or raw["pdf_url"])


def _recency_key(raw: dict[str, object]) -> tuple[str, str, str]:
    """Sort key for 'latest wins': PDF fetch, then metadata, then listing time.

    ISO 8601 strings compare chronologically; ``None`` becomes ``""`` (oldest).
    """
    return (
        str(raw["pdf_retrieved_at"] or ""),
        str(raw["meta_retrieved_at"] or ""),
        str(raw["listed_at"] or ""),
    )


def merge_runs(run_dirs: Iterable[Path]) -> MergeResult:
    """Merge runs, deduping by document identity and detecting content conflicts."""
    best: dict[str, tuple[tuple[str, str, str], dict[str, object], Derived, str]] = {}
    sha_seen: dict[str, dict[str, str]] = {}  # identity -> {sha256: run_name}
    run_names: list[str] = []

    for run_dir in run_dirs:
        run_name = run_dir.name
        run_names.append(run_name)
        for raw, derived in iter_records(run_dir):
            ident = _identity(raw)
            sha = raw["sha256"]
            if sha:
                sha_seen.setdefault(ident, {})[str(sha)] = run_name
            key = _recency_key(raw)
            current = best.get(ident)
            if current is None or key > current[0]:
                best[ident] = (key, raw, derived, run_name)

    conflicts = [
        Conflict(
            document_uuid=ident,
            sha256s=tuple(sorted(shas)),
            runs=tuple(sorted(set(shas.values()))),
        )
        for ident, shas in sorted(sha_seen.items())
        if len(shas) > 1
    ]

    # Deterministic order: by citation then document id, so the bag is stable.
    ordered = sorted(
        best.values(),
        key=lambda t: (
            str(t[1]["neutral_citation"] or ""),
            str(t[1]["document_uuid"] or ""),
        ),
    )
    pairs = [(raw, derived) for _, raw, derived, _ in ordered]
    source_run = {_identity(raw): run for _, raw, _, run in best.values()}
    return MergeResult(pairs, source_run, conflicts, run_names)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_manifest(run_dir: Path) -> dict[str, object]:
    path = run_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _merge_sources(run_dirs: list[Path]) -> tuple[str | None, list[str]]:
    """Union base_url and courts across the source runs' manifests."""
    base_url: str | None = None
    courts: list[str] = []
    for run_dir in run_dirs:
        manifest = _read_manifest(run_dir)
        if base_url is None and isinstance(manifest.get("base_url"), str):
            base_url = str(manifest["base_url"])
        manifest_courts = manifest.get("courts")
        if isinstance(manifest_courts, list):
            for court in manifest_courts:
                if court not in courts:
                    courts.append(str(court))
    return base_url, courts


def _assign_bag_filenames(merge: MergeResult) -> dict[str, str]:
    """Give each downloaded winner a bag-unique filename, rewriting ``filename``.

    Filenames are unique only within one run; the merged bag flattens PDFs from
    many runs into a single folder, so two distinct documents can share a name.
    On collision we assign a deterministic ``__N`` suffix (pairs are already in a
    stable sorted order) so the exported ``filename`` column and the bagged file
    always agree and no PDF silently overwrites another. Returns identity ->
    original source filename, so the copy step can still find the run's file.
    """
    seen: set[str] = set()
    source_names: dict[str, str] = {}
    for raw, _ in merge.pairs:
        if raw["download_status"] != _DL_DONE or not raw["filename"]:
            continue
        ident = _identity(raw)
        original = str(raw["filename"])
        source_names[ident] = original
        bag_name = original
        if bag_name in seen:
            stem, dot, ext = original.partition(".")
            n = 2
            while (candidate := f"{stem}__{n}{dot}{ext}") in seen:
                n += 1
            bag_name = candidate
        seen.add(bag_name)
        raw["filename"] = bag_name
    return source_names


def _copy_pdfs(
    merge: MergeResult, run_dirs: list[Path], dest: Path, source_names: dict[str, str]
) -> tuple[list[Path], list[str]]:
    """Copy each downloaded winner's PDF into ``dest`` under its bag-unique name.

    Returns the copied paths and the filenames that were expected (download done)
    but missing on disk -- a fixity gap the caller surfaces, never hides.
    """
    by_name = {d.name: d for d in run_dirs}
    copied: list[Path] = []
    missing: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    for raw, _ in merge.pairs:
        if raw["download_status"] != _DL_DONE or not raw["filename"]:
            continue
        ident = _identity(raw)
        original = source_names[ident]
        bag_name = str(raw["filename"])  # already made unique by _assign_bag_filenames
        run_dir = by_name[merge.source_run[ident]]
        # Defence in depth: a tampered DB filename must not read or write outside
        # the run's pdfs/ or the bag payload (matches run.py's download boundary).
        try:
            source = safe_output_path(run_dir / "pdfs", original)
            target = safe_output_path(dest, bag_name)
        except UnsafeFilenameError:
            missing.append(original)
            continue
        if not source.exists():
            missing.append(original)
            continue
        shutil.copy2(source, target)
        copied.append(target)
    return copied, missing


def _collect_revisions(run_dirs: list[Path]) -> list[dict[str, object]]:
    """Aggregate per-document PDF version changes across runs for the snapshot.

    Reads each run's append-only ``pdf_version`` history (tolerating older runs
    with no such table) and, for every document that gained a new version, emits a
    revision entry pairing the superseded bytes with the ones that replaced them --
    the audit trail that ``update --revalidate`` records in the single-run model.
    Deduplicated across runs by ``(document, old_sha, new_sha)`` so re-merging the
    same runs is stable, and ordered deterministically for a reproducible snapshot.
    """
    seen: set[tuple[str, str, str]] = set()
    entries: list[dict[str, object]] = []
    for run_dir in run_dirs:
        try:
            versions = read_pdf_versions(run_dir / "judgments.sqlite")
        except FileNotFoundError:
            continue
        by_record: dict[int, list[sqlite3.Row]] = {}
        for v in versions:
            by_record.setdefault(int(v["record_id"]), []).append(v)
        for record_versions in by_record.values():
            for older, newer in pairwise(record_versions):
                ident = str(older["document_uuid"] or older["pdf_url"])
                key = (ident, str(older["sha256"]), str(newer["sha256"]))
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    {
                        "document_uuid": older["document_uuid"],
                        "pdf_url": older["pdf_url"],
                        "neutral_citation": older["neutral_citation"],
                        "old_sha256": older["sha256"],
                        "old_bytes": older["bytes"],
                        "old_fetched_at": older["fetched_at"],
                        "old_filename": older["filename"],
                        "new_sha256": newer["sha256"],
                        "new_bytes": newer["bytes"],
                        "detected_at": older["superseded_at"],
                        "run": run_dir.name,
                    }
                )
    entries.sort(
        key=lambda e: (
            str(e["neutral_citation"] or ""),
            str(e["document_uuid"] or ""),
            str(e["detected_at"] or ""),
        )
    )
    return entries


def _copy_versions(run_dirs: list[Path], dest: Path) -> int:
    """Copy every run's archived superseded PDFs into the bag payload.

    Files are content-addressed (``<sha256>.pdf``), so identical names across runs
    are identical bytes and a dedup by name is safe. They land under
    ``data/pdfs/versions/`` and are hashed into ``manifest-sha256.txt`` like any
    payload file, so a published corpus carries -- and attests -- the superseded
    versions, not just a record that they changed. Returns the count copied.
    """
    copied = 0
    for run_dir in run_dirs:
        versions_dir = run_dir / "pdfs" / "versions"
        if not versions_dir.is_dir():
            continue
        for src in sorted(versions_dir.glob("*.pdf")):
            try:
                target = safe_output_path(dest, src.name)
            except UnsafeFilenameError:
                continue
            if target.exists():
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied += 1
    return copied


def _snapshot(
    merge: MergeResult, record_count: int, revisions: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "created": _now_iso(),
        "tool_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "derive_version": DERIVE_VERSION,
        "source_runs": merge.run_names,
        "record_count": record_count,
        "conflict_count": len(merge.conflicts),
        "conflicts": [
            {
                "document_uuid": c.document_uuid,
                "sha256s": list(c.sha256s),
                "runs": list(c.runs),
            }
            for c in merge.conflicts
        ],
        # Documents re-fetched and found changed since first archived. The full
        # count is authoritative; ``entries`` is truncated to keep the snapshot
        # bounded (superseded PDFs themselves travel in the bag under pdfs/versions/).
        "revisions": {
            "count": len(revisions),
            "truncated": len(revisions) > _MAX_SNAPSHOT_REVISIONS,
            "entries": revisions[:_MAX_SNAPSHOT_REVISIONS],
        },
    }


def _dataset_jsonld(descriptor: dict[str, object]) -> dict[str, object]:
    coverage = descriptor.get("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
    lo, hi = coverage.get("date_delivered_min"), coverage.get("date_delivered_max")
    temporal = f"{lo}/{hi}" if lo and hi else None
    # Point the distribution at the resource the descriptor actually wrote, not a
    # hard-coded CSV that may not exist for a json/parquet-only export.
    resources = descriptor.get("resources")
    resource = resources[0] if isinstance(resources, list) and resources else {}
    content_url = resource.get("path", "judgments.csv")
    encoding = resource.get("mediatype", "text/csv")
    return {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "name": "Courts Service of Ireland -- written judgments",
        "description": (
            "Metadata and PDFs of written judgments published by the Courts "
            "Service of Ireland, archived as a research dataset."
        ),
        "creator": {"@type": "SoftwareApplication", "name": "courts-scraper"},
        "license": "https://ww2.courts.ie",
        "isAccessibleForFree": True,
        "spatialCoverage": "Ireland",
        "temporalCoverage": temporal,
        "variableMeasured": [
            {"@type": "PropertyValue", "name": f.name, "description": f.title}
            for f in EXPORT_FIELDS
        ],
        "distribution": [
            {
                "@type": "DataDownload",
                "encodingFormat": encoding,
                "contentUrl": content_url,
            }
        ],
    }


def _datasheet(merge: MergeResult, record_count: int, missing: list[str]) -> str:
    runs = ", ".join(merge.run_names) or "(none)"
    conflicts = len(merge.conflicts)
    return f"""# Datasheet: Courts Service of Ireland judgments

Generated by courts-scraper {__version__} on {_now_iso()}.

## Motivation
A research dataset of written judgments published by the Courts Service of
Ireland (ww2.courts.ie), assembled for legal and computational research.

## Composition
- Records: {record_count} (one per judgment document/opinion).
- Source runs merged: {runs}.
- Deduplicated by document_uuid; latest fetch wins.
- Content conflicts (same document_uuid, differing sha256): {conflicts}. See
  snapshot.json for details.
- PDFs expected but missing on disk at bag time: {len(missing)}.

## Collection process
Collected by scraping the public search results and per-judgment view pages,
then downloading each PDF with checksum verification. Politeness spacing and
retry/backoff are applied; see each run's manifest.json for query and settings.

## Preprocessing / labelling
Dates normalised to ISO 8601. Derived fields (authoring_judge, panel, status/
result vocabulary checks) computed at export time from raw captured values; see
datapackage.json for the column dictionary. ECLI is deliberately not derived --
Ireland has no confirmed public ECLI scheme.

## Uses
Suitable for legal-research metadata analysis and NLP over the judgment PDFs.
Verify any citable fact against the official record at ww2.courts.ie.

## Distribution
Judicial judgments of Ireland are public domain. Personal data within a judgment
may still carry data-protection obligations or reporting restrictions; verify
before reprocessing or onward publication.

## Maintenance
Regenerate by re-running the scraper and `courts-scraper corpus`. Fixity is
verifiable via manifest-sha256.txt (BagIt).
"""


def _write_bagit(bag_dir: Path, data_dir: Path, merge: MergeResult) -> None:
    """Write BagIt tag files and fixity manifests over the payload in ``data/``."""
    (bag_dir / "bagit.txt").write_text(
        f"BagIt-Version: {_BAGIT_VERSION}\nTag-File-Character-Encoding: UTF-8\n",
        encoding="utf-8",
    )
    bag_info = (
        f"Source-Organization: courts-scraper\n"
        f"Bagging-Date: {datetime.now(UTC).date().isoformat()}\n"
        f"Bag-Software-Agent: courts-scraper {__version__}\n"
        f"External-Description: Courts Service of Ireland judgments corpus\n"
        f"Payload-Oxum: {_oxum(data_dir)}\n"
    )
    (bag_dir / "bag-info.txt").write_text(bag_info, encoding="utf-8")

    # Payload manifest: hash every file under data/, path relative to the bag.
    manifest_lines = []
    for path in sorted(p for p in data_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(bag_dir).as_posix()
        manifest_lines.append(f"{sha256_of(path)}  {rel}")
    (bag_dir / "manifest-sha256.txt").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8"
    )

    # Tag manifest: hash the tag files too (so the whole bag is verifiable).
    tag_lines = []
    for name in ("bagit.txt", "bag-info.txt", "manifest-sha256.txt"):
        tag_lines.append(f"{sha256_of(bag_dir / name)}  {name}")
    (bag_dir / "tagmanifest-sha256.txt").write_text(
        "\n".join(tag_lines) + "\n", encoding="utf-8"
    )


def _oxum(data_dir: Path) -> str:
    """BagIt Payload-Oxum: ``<octet count>.<file count>`` over the payload."""
    octets = count = 0
    for path in data_dir.rglob("*"):
        if path.is_file():
            octets += path.stat().st_size
            count += 1
    return f"{octets}.{count}"


def build_corpus(
    run_dirs: Iterable[Path],
    out_dir: Path,
    *,
    formats: Iterable[str] = ("csv", "json"),
) -> CorpusResult:
    """Merge ``run_dirs`` into a BagIt corpus bundle at ``out_dir``.

    Raises:
        ExportError: If no runs are given, or an unknown/unavailable format.
    """
    runs = list(run_dirs)
    if not runs:
        raise ExportError("no runs to merge into a corpus.")

    merge = merge_runs(runs)
    record_count = len(merge.pairs)
    base_url, courts = _merge_sources(runs)
    # Assign bag-unique filenames before writing the tabular package, so the CSV
    # 'filename' column matches the (collision-free) bagged files.
    source_names = _assign_bag_filenames(merge)

    data_dir = out_dir / "data"
    # Start from a clean payload: the manifest hashes every file under data/, so
    # leftovers from a prior build would be attested into this snapshot's fixity.
    if data_dir.exists():
        shutil.rmtree(data_dir)
    revisions = _collect_revisions(runs)
    snapshot = _snapshot(merge, record_count, revisions)

    # Tabular package (reuses the exact export writers) into the bag payload.
    write_package(
        merge.pairs,
        data_dir,
        formats=formats,
        base_url=base_url,
        courts=courts,
        extra={"snapshot": snapshot},
    )

    # PDFs + docs into the payload (latest of each, plus archived superseded ones).
    _copied, missing = _copy_pdfs(merge, runs, data_dir / "pdfs", source_names)
    _copy_versions(runs, data_dir / "pdfs" / "versions")

    descriptor = json.loads((data_dir / "datapackage.json").read_text(encoding="utf-8"))
    (data_dir / "dataset.jsonld").write_text(
        json.dumps(_dataset_jsonld(descriptor), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (data_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (data_dir / "DATASHEET.md").write_text(
        _datasheet(merge, record_count, missing), encoding="utf-8"
    )

    # Fixity last, so the manifest covers every payload file written above.
    _write_bagit(out_dir, data_dir, merge)

    return CorpusResult(
        bag_dir=out_dir,
        record_count=record_count,
        conflicts=tuple(merge.conflicts),
        missing_pdfs=tuple(missing),
    )
