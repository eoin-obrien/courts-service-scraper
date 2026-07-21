# courts-scraper

[![CI](https://github.com/eoin-obrien/courts-service-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/eoin-obrien/courts-service-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/badge/lint-ruff-000000)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2)](https://mypy-lang.org/)
[![DOI](https://zenodo.org/badge/1307155551.svg)](https://doi.org/10.5281/zenodo.21465515)

A research tool that archives written judgments published by the
**Courts Service of Ireland** at [`ww2.courts.ie`](https://ww2.courts.ie).

It builds a queryable SQLite database of judgment metadata and downloads the
judgment PDFs into a self-contained, resumable data folder. It ships with
Supreme Court support and is designed so other courts (Court of Appeal, High
Court, ...) can be added by extending a single enum.

## What it collects

For every judgment in the search results it records:

- Date Delivered, Title, Court, Judge, Date Uploaded (dates normalised to
  ISO 8601 from the Irish `dd/mm/yyyy` / `02 July 2026` formats)
- The judgment view-page URL and the direct PDF URL
- Authoritative metadata from the view page: **Neutral Citation** (mandatory),
  Record Number, Status, Result, Composition of the Court, and every other
  labelled field, archived verbatim as JSON

Each PDF is named from its Neutral Citation and authoring judge, e.g.
`2026_IESC_36_Woulfe-J.pdf`. A judgment with multiple opinions produces one
file per opinion.

## How it works

Two phases over a per-run data folder:

1. **List** -- iterate the paginated search results into the database.
2. **Download** -- for each row, scrape the view page for metadata, then
   download the PDF with atomic, checksum-verified, resumable writes.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev      # create the environment and install dependencies
```

## Usage

```bash
# List Supreme Court results into a new run folder under ./data
uv run courts-scraper list --court supreme

# See existing runs and their progress
uv run courts-scraper runs

# Resume a run (pick it interactively, or name it, or take the newest)
uv run courts-scraper download            # prompts you to choose a run
uv run courts-scraper download --latest   # newest run, no prompt
uv run courts-scraper download --run-dir data/<timestamp>__supreme

# Or do both phases at once
uv run courts-scraper run --court supreme

# Check progress at any time
uv run courts-scraper status              # also picks a run if not given

# Export one run to a Frictionless Data Package (CSV + JSON + optional Parquet)
uv run courts-scraper export --latest --format csv,json,parquet

# Merge every run into one citable, fixity-checked corpus bundle (BagIt)
uv run courts-scraper corpus --out data/corpus

# Print the column data dictionary (generated from the schema)
uv run courts-scraper data-dictionary
```

Useful options: `--delay` / `--jitter` (politeness spacing, defaults 5s + 2s),
`--max-pages` and `--limit` (sampling for testing), `--court` (repeatable),
`--user-agent` (override the request User-Agent), `--yes` (skip confirmation),
`--latest` (resume the newest run unattended).

### Choosing courts and confirming

Scraping is deliberately not eager:

- **No `--court`?** You get a checkbox multiselect to pick courts (Supreme Court
  pre-selected). Pass `--court` one or more times to skip the prompt.
- **Before any crawl** the tool shows the scale (result count, page count,
  estimated time at the current politeness settings) and asks you to confirm.

For unattended runs (cron, CI, scripts) pass `--yes` to skip the confirmation:

```bash
uv run courts-scraper run --court supreme --yes
```

In a non-interactive session, the tool never hangs on a prompt: it requires
`--court` and `--yes` explicitly and errors clearly if either is missing.

### Discovering and resuming runs

`courts-scraper runs` lists every run under the data directory with its progress
(`done/total`, errors). To resume, `download`/`status` accept a run three ways:

- `--run-dir <folder>` names it explicitly;
- `--latest` takes the newest run without prompting (good for scripts);
- with neither, an interactive picker lists your runs (newest first) to choose.

In a non-interactive session the picker never hangs: it errors and tells you to
pass `--run-dir` or `--latest`.

### Cancel and resume

Press **Ctrl-C once** to stop cleanly. In-flight downloads are written to a
`.part` file and only atomically renamed to their final name once complete and
verified, so cancelling never leaves a half-file that a later run would mistake
for a finished download. Re-run `download` to resume exactly where you stopped.

Network errors (timeouts like `ReadTimeout`, connection drops, `429`/`5xx`) are
retried automatically with exponential backoff (tune with `--max-attempts`). If
the site goes down entirely (it occasionally does for tens of minutes during
document uploads), the scraper detects the outage after a few consecutive
failures, **pauses and re-probes on an escalating interval**, and resumes where
it left off once the site is back, rather than hammering a down server. If the
outage outlasts an hour it stops cleanly so you can resume later.

## Data folder layout

```
data/
  20260720T231500Z__supreme/
    manifest.json        # search query, courts, start time, tool version
    judgments.sqlite     # all metadata for this run
    pdfs/                # downloaded judgment PDFs
    logs/
      errors.log         # durable log of skipped/failed items for follow-up
```

Each record also carries per-phase provenance: when the search row was seen
(`listed_at`), when the view page was scraped (`meta_retrieved_at`), and when the
PDF was fetched and verified (`pdf_retrieved_at`), plus the PDF response's
`Last-Modified`/`ETag`/`Content-Type` headers. The database schema migrates on
open, so runs made by an older version keep working without a re-scrape.

## Research dataset outputs

The raw SQLite is captured truth. Two commands turn it into standards-based
research data; all derived fields (ECLI later, controlled-vocabulary checks,
authoring-judge vs full-panel) are computed at export time, so a fix is a
re-export, never a re-scrape.

- **`export`** writes a [Frictionless Data Package](https://datapackage.org):
  `judgments.csv`, `judgments.json` (with nested metadata and the full panel),
  optional `judgments.parquet`, a `datapackage.json` Table Schema, and a
  `DATA_DICTIONARY.md`. The primary key is `document_uuid`; neutral citation is
  emitted but is case-level and repeats across a case's opinions, so it is not a
  row key. The column dictionary lives at [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md).
- **`corpus`** merges every run into one publishable [BagIt](https://datatracker.ietf.org/doc/html/rfc8493)
  bundle: deduplicated by `document_uuid` (latest fetch wins), with a fresh
  `manifest-sha256.txt` over the whole payload. If the same document was ever
  served with a different checksum across runs, that mutation is reported in
  `snapshot.json`, never silently dropped. The bag also carries a schema.org
  `dataset.jsonld` (for [Google Dataset Search](https://datasetsearch.research.google.com/)),
  a `DATASHEET.md`, and a frozen `snapshot.json` stamping the schema/derive
  versions and source-run set, so a Zenodo DOI pins one immutable snapshot.

Parquet needs the optional extra: `uv sync --extra parquet`.

## Development

```bash
uv run ruff format .     # format
uv run ruff check .      # lint
uv run mypy              # type-check
uv run pytest            # test suite (no network required)
```

The test suite parses saved HTML fixtures and mocks HTTP, so it fully validates
parsing, naming, date handling, resume logic and the atomic-download guarantees
before any real download is performed.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/),
enforced by a [commitizen](https://commitizen-tools.github.io/commitizen/)
pre-commit hook (`uv run cz commit` to author one). See
[CONTRIBUTING.md](CONTRIBUTING.md), the [Code of Conduct](CODE_OF_CONDUCT.md),
and the [security policy](SECURITY.md).

## Politeness and access

The tool targets a public government website. It serialises requests and spaces
them out by default. `robots.txt` is not consulted; the judgments are public
records and requests are made conservatively. Use responsibly and adjust
`--delay` upward for large crawls.

## Disclaimer and responsible use

This is an independent research tool. It is **not affiliated with, endorsed by,
or supported by the Courts Service of Ireland.**

- **Responsibility.** You are responsible for using this tool lawfully and for
  complying with the source website's terms of use and applicable law. It does
  not consult `robots.txt` (see above); that choice, and how you use the tool,
  are yours.
- **Personal data.** Judgments may contain personal information. If you process
  or redistribute downloaded material, you are responsible for complying with
  data-protection law (e.g. GDPR) and any reporting restrictions.
- **Accuracy.** Scraped metadata may be incomplete or incorrect. For any
  authoritative or citable use, verify against the official record at
  [`ww2.courts.ie`](https://ww2.courts.ie). The downloaded PDFs are archived
  exactly as served.
- **No warranty.** This software is provided "as is", without warranty of any
  kind. The authors accept no liability for its use. See [LICENSE](LICENSE).

## Citing

If you use `courts-scraper` in research, please cite it. Every release is
archived on [Zenodo](https://doi.org/10.5281/zenodo.21465515) with a DOI, and
GitHub's **"Cite this repository"** button (powered by
[`CITATION.cff`](CITATION.cff)) produces a formatted citation.

> O'Brien, E. (2026). *courts-scraper: a research scraper for Courts Service of
> Ireland judgments* (Version 0.1.0) [Computer software]. Zenodo.
> https://doi.org/10.5281/zenodo.21465515

The DOI [`10.5281/zenodo.21465515`](https://doi.org/10.5281/zenodo.21465515)
always resolves to the latest release; each individual release also has its own
version-specific DOI on its Zenodo record.

## License

Released under the [MIT License](LICENSE).
