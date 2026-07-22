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
# Crawl Supreme Court judgments into a new run folder under ./data (list + download)
uv run courts-scraper fetch --court supreme

# Just record the search results, no PDFs yet (the old `list`)
uv run courts-scraper fetch --court supreme --list-only

# Resume a run -- "resume" is just running fetch again
uv run courts-scraper fetch                    # interactive: start new OR pick a run
uv run courts-scraper fetch --latest           # resume the newest run, no prompt
uv run courts-scraper fetch --run-dir data/<timestamp>__supreme

# See existing runs and their progress
uv run courts-scraper runs
uv run courts-scraper runs --json              # machine-readable

# Keep a canonical run current: fetch only newly-published judgments
uv run courts-scraper update --latest
uv run courts-scraper update --run-dir data/<timestamp>__supreme --yes
uv run courts-scraper update --latest --json   # {new, revisions, errors, run}

# Also re-check already-downloaded PDFs for server-side changes (costly, opt-in)
uv run courts-scraper update --latest --revalidate --yes

# Check progress at any time
uv run courts-scraper status                   # also picks a run if not given
uv run courts-scraper status --latest --json   # counts as JSON

# Export one run to a Frictionless Data Package (CSV + JSON + optional Parquet)
uv run courts-scraper export --latest --format csv,json,parquet

# Merge every run into one citable, fixity-checked corpus bundle (BagIt)
uv run courts-scraper corpus --out data/corpus

# Print the column data dictionary (generated from the schema)
uv run courts-scraper dictionary
```

`--data-dir` is a **global** option (default `./data`, or `COURTS_SCRAPER_DATA`).
Being global it must come *before* the subcommand:
`uv run courts-scraper --data-dir ./mydata runs` (not after it).

Useful options: `--delay` / `--jitter` (politeness spacing, defaults 5s + 2s),
`--max-pages` and `--limit` (sampling for testing), `--court`/`-c` (repeatable),
`--list-only` (record results without downloading), `--user-agent` (override the
request User-Agent), `--yes` (skip confirmation), `--latest` (act on the newest
run unattended), `--json` (machine-readable output on `status`/`runs`/`export`/
`corpus`/`update`), `--revalidate` (on `update`, also re-fetch downloaded PDFs to
detect and version server-side changes).

`fetch` vs `update`: `fetch` **starts or finishes** a crawl -- it creates a new run
(with `--court`) or resumes an incomplete one (with `--run-dir`/`--latest`),
resolving metadata and fetching PDFs for rows still pending. `update` **maintains a
complete run** over time -- it re-lists the run's fixed search so only genuinely-new
judgments become pending, then fetches just those. Use `fetch` to create or complete
a run; use `update` to keep it current. `fetch --court` refuses to re-crawl a run
that is already complete and points you at `update`.

Exit codes: `0` success (including a clean first-Ctrl-C stop), `1` outage/error,
`2` bad usage, `130` a second Ctrl-C, `143` SIGTERM.

### Watching a crawl

A crawl of thousands of judgments is polite, so it is mostly *waiting* on purpose.
In a wide interactive terminal `fetch`/`update` show a live dashboard that makes that
legible: overall progress, an **ETA and finish time**, the current judgment, a
**countdown to the next request** (so a paused-for-politeness crawl never looks hung),
and a loud banner when the site is down and the crawler is backing off. When output is
piped, run under cron, on a terminal narrower than 80 columns, or with `--quiet`, it
falls back to plain periodic status lines (never an animated bar smeared into a log),
so the same progress is readable in a log file. `NO_COLOR` is honoured and the glyphs
degrade to ASCII on non-UTF-8 terminals. The `--json` output of `status`/`update`/etc.
is unchanged -- still exactly one JSON document on stdout.

### Choosing courts and confirming

Scraping is deliberately not eager:

- **No `--court`?** You get a checkbox multiselect to pick courts (Supreme Court
  pre-selected). Pass `--court` one or more times to skip the prompt.
- **Before any crawl** the tool shows the scale (result count, page count,
  estimated time at the current politeness settings) and asks you to confirm.

For unattended runs (cron, CI, scripts) pass `--yes` to skip the confirmation:

```bash
uv run courts-scraper fetch --court supreme --yes
```

In a non-interactive session, the tool never hangs on a prompt: it requires
`--court` and `--yes` explicitly and errors clearly if either is missing.

### Discovering and resuming runs

`courts-scraper runs` lists every run under the data directory with its progress
(`done/total`, errors). To resume, `fetch`/`status`/`update`/`export` accept a run
three ways:

- `--run-dir <folder>` names it explicitly;
- `--latest` takes the newest run without prompting (good for scripts);
- with neither, an interactive picker lists your runs (newest first) to choose.

In a non-interactive session the picker never hangs: it errors and tells you to
pass `--run-dir` or `--latest` (or `--court` to start a new run).

### Cancel and resume

Press **Ctrl-C once** to stop cleanly. In-flight downloads are written to a
`.part` file and only atomically renamed to their final name once complete and
verified, so cancelling never leaves a half-file that a later run would mistake
for a finished download. Re-run `fetch --latest` to resume exactly where you stopped.

Network errors (timeouts like `ReadTimeout`, connection drops, `429`/`5xx`) are
retried automatically with exponential backoff (tune with `--max-attempts`). If
the site goes down entirely (it occasionally does for tens of minutes during
document uploads), the scraper detects the outage after a few consecutive
failures, **pauses and re-probes on an escalating interval**, and resumes where
it left off once the site is back, rather than hammering a down server. If the
outage outlasts an hour it stops cleanly so you can resume later.

### Keeping a corpus current (evergreen updates)

Instead of a fresh full crawl each period (which re-downloads the entire corpus
and is impolite to a government server), keep **one canonical run per
court-selection** and `update` it on a schedule:

```bash
# One-time: create the canonical run.
uv run courts-scraper fetch --court supreme --yes

# Then, on a cadence (cron), fetch only what is new. Use a higher --delay for a
# scheduled job -- incremental fetch means most runs touch very little.
uv run courts-scraper update --run-dir data/<timestamp>__supreme --yes --delay 10

# Rebuild the citable corpus after updating, and mint a new Zenodo *version*
# (same concept-DOI) on your chosen cadence -- not every run.
uv run courts-scraper corpus --out data/corpus
```

A cron line for a nightly incremental update:

```cron
0 3 * * *  cd /path/to/courts-scraper && uv run courts-scraper update \
             --run-dir data/20260720T231500Z__supreme --yes --delay 10
```

`update` re-uses the same politeness spacing, retry/backoff, and outage
circuit-breaker as every other command, so a scheduled job is safe against the
site's occasional upload-window downtime -- it pauses and resumes rather than
failing the run.

**`--revalidate` (opt-in, costly).** The Courts Service server exposes no HTTP
cache validators (no `ETag`/`Last-Modified`, and conditional requests never
`304`), so there is no cheap way to ask "did this document change?" -- detecting
a change means re-fetching the file. `update --revalidate` therefore re-downloads
every already-fetched PDF, and the command tells you the size of that up front and
refuses to run unattended without `--yes`. When a document **has** changed it
never overwrites the old version: the previous bytes are archived under
`pdfs/versions/<sha256>.pdf`, the new bytes become the live file, and the change
is recorded in the append-only version history (surfaced in the corpus
`snapshot.json` under `revisions`, with the superseded PDFs carried into the bag
under fixity). Reserve it for a slower cadence than the incremental `update`.

## Data folder layout

```
data/
  20260720T231500Z__supreme/
    manifest.json        # search query, courts, start time, tool version,
                         #   and listing completeness (--max-pages truncation)
    judgments.sqlite     # all metadata + the per-document PDF version history
    pdfs/                # downloaded judgment PDFs (latest version of each)
      versions/          # superseded PDF versions, named by sha256 (revalidate)
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
