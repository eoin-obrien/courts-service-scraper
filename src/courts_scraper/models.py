"""Typed data structures shared across the scraper.

These dataclasses model the two things we parse out of the website -- a single
row of the paginated search results (:class:`ListRow`) and the richer metadata
found on an individual judgment's view page (:class:`JudgmentMeta`) -- plus the
immutable configuration for one run (:class:`RunConfig`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ListRow:
    """One row of the search results table.

    The results are enumerated per document, so a single judgment that carries
    several opinions (e.g. a concurring and a dissenting judgment) appears as
    several rows that share ``collection_uuid`` and ``title``.

    Attributes:
        page: Zero-based search page the row was found on.
        title: Case title, e.g. ``"O'Donnell -v- Dublin City Council"``.
        court: Court name exactly as shown, e.g. ``"Supreme Court"``.
        judge: Authoring judge label, e.g. ``"Woulfe J."`` (may be empty).
        date_delivered: Delivery date in ISO 8601 (``YYYY-MM-DD``) or ``None``.
        date_uploaded: Upload date in ISO 8601 (``YYYY-MM-DD``) or ``None``.
        view_url: Absolute URL of the judgment view page.
        pdf_url: Absolute direct-download URL of the PDF (fragment stripped).
        collection_uuid: Alfresco collection id grouping a judgment's documents.
        document_uuid: Alfresco id of this specific document.
    """

    page: int
    title: str
    court: str
    judge: str
    date_delivered: str | None
    date_uploaded: str | None
    view_url: str
    pdf_url: str
    collection_uuid: str
    document_uuid: str


@dataclass(frozen=True, slots=True)
class SupplementaryDoc:
    """A non-primary document linked from a view page (memo, summary, docx...).

    We record these for provenance but do not download them by default.
    """

    label: str
    url: str


@dataclass(frozen=True, slots=True)
class JudgmentMeta:
    """Key/value metadata scraped from a judgment view page.

    Attributes:
        neutral_citation: e.g. ``"[2026] IESC 36"``. Mandatory; a missing value
            is treated as an error for the row (see the run orchestration).
        fields: The full label->value map from the page's metadata cells,
            captured verbatim for research use and archived as JSON.
        supplementary: Links to related documents (signed copies, memos,
            summaries, Word versions) that we record but do not download.
    """

    neutral_citation: str | None
    fields: dict[str, str] = field(default_factory=dict)
    supplementary: list[SupplementaryDoc] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Immutable configuration describing a single scraping run.

    Attributes:
        run_dir: Root folder that holds this run's database, PDFs and logs.
        base_url: Site origin, e.g. ``https://ww2.courts.ie``.
        query: The encoded courts.ie search query string.
        courts: Human-readable court names included in the search.
        delay: Minimum seconds between outbound requests (politeness).
        jitter: Maximum extra random seconds added to ``delay``.
        max_attempts: Retry attempts per request before giving up.
        timeout: Per-request timeout in seconds.
        user_agent: User-Agent header sent with every request.
    """

    run_dir: Path
    base_url: str
    query: str
    courts: tuple[str, ...]
    delay: float
    jitter: float
    max_attempts: int
    timeout: float
    user_agent: str

    @property
    def db_path(self) -> Path:
        """Path to this run's SQLite database."""
        return self.run_dir / "judgments.sqlite"

    @property
    def pdf_dir(self) -> Path:
        """Directory that holds downloaded PDFs."""
        return self.run_dir / "pdfs"

    @property
    def log_dir(self) -> Path:
        """Directory that holds run and error logs."""
        return self.run_dir / "logs"

    @property
    def error_log_path(self) -> Path:
        """Durable error log for manual follow-up (e.g. missing citations)."""
        return self.log_dir / "errors.log"

    @property
    def manifest_path(self) -> Path:
        """Path to the run's ``manifest.json`` describing what was requested."""
        return self.run_dir / "manifest.json"
