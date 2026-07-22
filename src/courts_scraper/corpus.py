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
from pathlib import Path, PurePosixPath

from courts_scraper import __version__
from courts_scraper.dataset import DERIVE_VERSION, Derived, iter_records
from courts_scraper.db import SCHEMA_VERSION, iter_pdf_versions
from courts_scraper.download import sha256_of
from courts_scraper.export import EXPORT_FIELDS, ExportError, write_package
from courts_scraper.naming import UnsafeFilenameError, safe_output_path

_BAGIT_VERSION = "1.0"
_DL_DONE = "done"
# Cap on revision entries embedded verbatim in snapshot.json. The cap bounds the
# *build*, not just the output: :func:`_collect_revisions` stops accumulating
# entry dicts once it holds this many, while still counting the true total by
# streaming the ``pdf_version`` history. So a corpus with a very long (or, from a
# hostile run, adversarial) revision history cannot bloat or OOM the bag -- the
# embedded list is a bounded, deterministic sample and the count stays authoritative.
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
    # Referenced superseded versions that could not be verified (missing on disk or
    # whose bytes did not re-hash to their content-addressed name) and so were
    # omitted from the bag. Surfaced, never silently dropped.
    unverified_versions: tuple[str, ...] = ()


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


# Memory scope call (P1, deliberately deferred). merge_runs holds the whole
# deduplicated corpus in RAM: one ``best`` entry per surviving document (recency
# key + raw dict + Derived + run name), plus a ``sha_seen`` map per identity, and
# write_package then materialises ``flat`` (and, for JSON, ``nested``) lists over
# the same set. Deduping by identity and emitting a *globally sorted* bag both
# inherently require holding every surviving identity at once, so a true streamed
# write would mean spilling the dedup/sort index to disk (a temp SQLite or an
# external merge sort) -- a corpus-read-side rewrite touching write_package's row
# buffering. Budget: a ~50k-record corpus is ~150-250 MB peak here (order 3-5 KB
# per record across best + flat + nested); comfortable on a laptop, tight on a
# 1 GB single-board machine, and the term grows linearly. Trigger to convert:
# when a target corpus exceeds ~100k records, or must build on <2 GB RAM. Until
# then the revision aggregation (below) is the streamed path, since its size is
# bounded by *content changes*, not corpus size, and a hostile history could
# otherwise dwarf the corpus itself.
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


@dataclass(frozen=True, slots=True)
class _RevisionScan:
    """A bounded sample of revision entries plus the true (unbounded) total.

    ``entries`` holds at most :data:`_MAX_SNAPSHOT_REVISIONS` dicts; ``total`` is
    the count of *distinct* revisions across all runs, whatever the sample size.
    """

    entries: list[dict[str, object]]
    total: int


def _collect_revisions(run_dirs: list[Path]) -> _RevisionScan:
    """Aggregate per-document PDF version changes across runs for the snapshot.

    Streams each run's append-only ``pdf_version`` history (tolerating older runs
    with no such table) rather than materialising it, and, for every document that
    gained a new version, emits a revision entry pairing the superseded bytes with
    the ones that replaced them -- the audit trail that ``update --revalidate``
    records in the single-run model. History rows arrive ordered by
    ``(record_id, fetched_at, id)``, so a record's versions are contiguous and can
    be paired on the fly by holding only the previous row.

    Deduplicated across runs by ``(document, old_sha, new_sha)`` so re-merging the
    same runs is stable. Memory is bounded regardless of history size: entry dicts
    stop accumulating once the sample reaches :data:`_MAX_SNAPSHOT_REVISIONS`,
    while ``total`` still counts every distinct revision (the ``seen`` set it needs
    for correct cross-run dedup is O(distinct revisions), i.e. content changes, not
    corpus size). The kept sample is sorted deterministically for a reproducible
    snapshot; being capped by scan order it is a stable sample, not the global top-N.
    """
    seen: set[tuple[str, str, str]] = set()
    entries: list[dict[str, object]] = []
    total = 0
    for run_dir in run_dirs:
        record_id: int | None = None
        prev: sqlite3.Row | None = None
        try:
            for v in iter_pdf_versions(run_dir / "judgments.sqlite"):
                rid = int(v["record_id"])
                if rid != record_id:
                    # First row of a new record group: nothing precedes it to
                    # supersede, so it only seeds ``prev`` for the next pairing.
                    record_id, prev = rid, v
                    continue
                assert prev is not None  # set by the group's first row, above
                older, newer = prev, v
                prev = v
                ident = str(older["document_uuid"] or older["pdf_url"])
                key = (ident, str(older["sha256"]), str(newer["sha256"]))
                if key in seen:
                    continue
                seen.add(key)
                total += 1
                if len(entries) < _MAX_SNAPSHOT_REVISIONS:
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
        except FileNotFoundError:
            continue
    entries.sort(
        key=lambda e: (
            str(e["neutral_citation"] or ""),
            str(e["document_uuid"] or ""),
            str(e["detected_at"] or ""),
        )
    )
    return _RevisionScan(entries=entries, total=total)


def _referenced_versions(db_path: Path) -> set[str]:
    """Basenames of the ``versions/*.pdf`` archives a superseded row in this DB cites.

    Only superseded ``pdf_version`` rows reference an archived file (the *current*
    version's bytes are the live PDF, copied by :func:`_copy_pdfs`), so this is the
    exact set the bag should carry -- anything else under ``pdfs/versions/`` is an
    orphan. Tolerates a missing/older/corrupt DB the same way the rest of the read
    path does. Only the basename is kept, so a tampered ``filename`` cannot smuggle
    a path in; :func:`safe_output_path` re-checks it at copy time regardless.
    """
    referenced: set[str] = set()
    try:
        for v in iter_pdf_versions(db_path):
            if v["superseded_at"] is None:
                continue
            filename = v["filename"]
            if filename:
                referenced.add(PurePosixPath(str(filename)).name)
    except FileNotFoundError:
        return referenced
    return referenced


def _copy_versions(run_dirs: list[Path], dest: Path) -> tuple[int, list[str]]:
    """Copy only *referenced, verified* superseded PDFs into the bag payload.

    A ``versions/<sha256>.pdf`` file is bagged only when a superseded
    ``pdf_version`` row points at it (so orphaned archive files left on disk are
    not shipped as unexplained payload) AND its bytes re-hash to the ``<sha256>``
    in its own name (so a corrupt archive file is never attested as authoritative
    by BagIt fixity). Files are content-addressed, so identical names across runs
    are identical bytes; we hash-and-copy each unique name at most once. Copied
    files land under ``data/pdfs/versions/`` and ride ``manifest-sha256.txt`` like
    any payload. Returns the count copied and the names that were referenced but
    could not be verified (missing on disk or digest mismatch) -- a gap the caller
    surfaces, never hides.
    """
    copied = 0
    done: set[str] = set()  # names already bagged (dedup across runs)
    failed: set[str] = set()  # referenced but unverifiable
    for run_dir in run_dirs:
        versions_dir = run_dir / "pdfs" / "versions"
        for name in sorted(_referenced_versions(run_dir / "judgments.sqlite")):
            if name in done:
                continue
            try:
                src = safe_output_path(versions_dir, name)
                target = safe_output_path(dest, name)
            except UnsafeFilenameError:
                failed.add(name)
                continue
            # Content-addressed: the trustworthy bytes are exactly those that hash
            # to the name. Anything that fails to match, or that vanishes / is
            # unreadable between the check and the read (concurrent prune, perms,
            # torn copy), surfaces as a gap -- it must never abort the whole bag,
            # matching iter_pdf_versions' "never abort a corpus build" tolerance.
            try:
                if not src.is_file() or sha256_of(src) != src.stem:
                    failed.add(name)
                    continue
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
            except OSError:
                # Drop any partial target so the manifest cannot attest torn bytes.
                target.unlink(missing_ok=True)
                failed.add(name)
                continue
            copied += 1
            done.add(name)
    # A name that verified in one run is not a gap even if a different run's copy
    # was missing/corrupt, so subtract what we managed to bag.
    return copied, sorted(failed - done)


def _source_listings(run_dirs: list[Path]) -> dict[str, object]:
    """Per-run listing coverage; ``sources`` is the authoritative record.

    A corpus merges many runs, each with its own ``listing`` block, and they can
    cover different court sets. There is no honest single corpus-level "complete"
    boolean: a full crawl of Court A plus a truncated crawl of Court B is complete
    for A and partial for B, so any one flag would lie about one of them. Rather
    than paper that over, the per-run ``sources`` map is the source of truth, and
    only two aggregates that are always exactly correct are exposed:

    * ``all_verified`` -- every source carried a verified ``listing`` block (older,
      pre-feature runs did not, so they read as unverified, never as full).
    * ``any_truncated`` -- at least one *verified* source was deliberately
      truncated, so the corpus is known to be missing pages for some court set.
    """
    sources: dict[str, object] = {}
    all_verified = True
    any_truncated = False
    for run_dir in run_dirs:
        block = _read_manifest(run_dir).get("listing")
        if isinstance(block, dict) and block.get("complete") is True:
            truncated = bool(block.get("truncated", False))
            sources[run_dir.name] = {
                "complete": True,
                "truncated": truncated,
                "pages_fetched": block.get("pages_fetched"),
                "pages_available": block.get("pages_available"),
            }
            any_truncated = any_truncated or truncated
        else:
            sources[run_dir.name] = {"verified": False}
            all_verified = False
    return {
        "sources": sources,
        "all_verified": all_verified,
        "any_truncated": any_truncated,
        "note": (
            "Per-source 'sources' is authoritative. Corpus-level completeness "
            "across differing court sets is not reduced to a single boolean; "
            "'any_truncated' means at least one verified source was capped."
        ),
    }


def _snapshot(
    merge: MergeResult,
    record_count: int,
    revisions: _RevisionScan,
    run_dirs: list[Path],
) -> dict[str, object]:
    return {
        "created": _now_iso(),
        "tool_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "derive_version": DERIVE_VERSION,
        "source_runs": merge.run_names,
        "listing": _source_listings(run_dirs),
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
        # Documents re-fetched and found changed since first archived. ``count`` is
        # the authoritative total; ``entries`` is a bounded, deterministic sample
        # (capped during aggregation, not just here) so a long history cannot bloat
        # the snapshot -- the superseded PDFs themselves travel in the bag under
        # pdfs/versions/.
        "revisions": {
            "count": revisions.total,
            "truncated": revisions.total > len(revisions.entries),
            "entries": revisions.entries,
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


def _datasheet(
    merge: MergeResult,
    record_count: int,
    missing: list[str],
    unverified_versions: list[str],
) -> str:
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
- Superseded versions referenced but unverifiable (missing or digest mismatch),
  so omitted from the bag: {len(unverified_versions)}.

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
    snapshot = _snapshot(merge, record_count, revisions, runs)

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
    _copied_versions, unverified_versions = _copy_versions(
        runs, data_dir / "pdfs" / "versions"
    )

    descriptor = json.loads((data_dir / "datapackage.json").read_text(encoding="utf-8"))
    (data_dir / "dataset.jsonld").write_text(
        json.dumps(_dataset_jsonld(descriptor), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (data_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (data_dir / "DATASHEET.md").write_text(
        _datasheet(merge, record_count, missing, unverified_versions),
        encoding="utf-8",
    )

    # Fixity last, so the manifest covers every payload file written above.
    _write_bagit(out_dir, data_dir, merge)

    return CorpusResult(
        bag_dir=out_dir,
        record_count=record_count,
        conflicts=tuple(merge.conflicts),
        missing_pdfs=tuple(missing),
        unverified_versions=tuple(unverified_versions),
    )


# ---------------------------------------------------------------------------
# Serialisation: bundle a finished bag into one shareable archive
# ---------------------------------------------------------------------------
# User-facing archive formats -> (shutil format name, file extension). Every
# format is pure stdlib (zipfile/tarfile); the aliases accept the common
# spellings a user is likely to type.
_ARCHIVE_FORMATS: dict[str, tuple[str, str]] = {
    "zip": ("zip", ".zip"),
    "tar": ("tar", ".tar"),
    "tar.gz": ("gztar", ".tar.gz"),
    "tgz": ("gztar", ".tar.gz"),
    "gztar": ("gztar", ".tar.gz"),
    "tar.bz2": ("bztar", ".tar.bz2"),
    "tbz2": ("bztar", ".tar.bz2"),
    "bztar": ("bztar", ".tar.bz2"),
    "tar.xz": ("xztar", ".tar.xz"),
    "txz": ("xztar", ".tar.xz"),
    "xztar": ("xztar", ".tar.xz"),
}

#: Canonical archive format names shown to users (one per distinct output).
ARCHIVE_FORMAT_CHOICES: tuple[str, ...] = ("zip", "tar", "tar.gz", "tar.bz2", "tar.xz")


def resolve_archive_format(fmt: str) -> tuple[str, str]:
    """Map a user-supplied format name to ``(shutil_format, extension)``.

    Accepts the canonical names in :data:`ARCHIVE_FORMAT_CHOICES` plus common
    aliases (``tgz``, ``tbz2``, ``txz``, a leading dot). Validates that the
    format is actually available in this Python build (the ``bz2``/``lzma``
    modules are optional at compile time).

    Raises:
        ExportError: If ``fmt`` is unknown or unavailable here.
    """
    key = fmt.strip().lower().lstrip(".")
    if key not in _ARCHIVE_FORMATS:
        choices = ", ".join(ARCHIVE_FORMAT_CHOICES)
        raise ExportError(f"unknown archive format {fmt!r}; choose one of: {choices}")
    shutil_fmt, ext = _ARCHIVE_FORMATS[key]
    available = {name for name, _desc in shutil.get_archive_formats()}
    if shutil_fmt not in available:
        raise ExportError(
            f"archive format {fmt!r} is unavailable in this Python build "
            f"(its compression module is missing)."
        )
    return shutil_fmt, ext


def serialize_bag(bag_dir: Path, fmt: str) -> tuple[Path, str]:
    """Serialise a finished bag directory into one shareable archive.

    Writes a single file alongside ``bag_dir`` whose only top-level entry is the
    bag directory -- the BagIt serialisation convention, so the result validates
    directly (e.g. ``bagit.py --validate corpus.tar.gz``). Also writes a
    ``<archive>.sha256`` sidecar in ``sha256sum`` format so a recipient can verify
    the download's integrity (the bag's own ``manifest-sha256.txt`` verifies its
    *contents*; this verifies the *transfer*). The digest is streamed, so a
    multi-gigabyte corpus never has to fit in memory.

    Args:
        bag_dir: The finished corpus bag directory (must exist).
        fmt: An archive format (see :func:`resolve_archive_format`).

    Returns:
        ``(archive_path, sha256_hex)``.

    Raises:
        ExportError: If ``bag_dir`` is missing, or ``fmt`` is unknown/unavailable.
    """
    if not bag_dir.is_dir():
        raise ExportError(f"no bag directory to archive at {bag_dir}.")
    shutil_fmt, _ext = resolve_archive_format(fmt)
    archive = Path(
        shutil.make_archive(
            base_name=str(bag_dir),
            format=shutil_fmt,
            root_dir=str(bag_dir.parent),
            base_dir=bag_dir.name,
        )
    )
    digest = sha256_of(archive)
    sidecar = archive.with_name(archive.name + ".sha256")
    sidecar.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, digest
