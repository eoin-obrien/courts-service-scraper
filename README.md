# courts-scraper

[![CI](https://github.com/eoin-obrien/courts-service-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/eoin-obrien/courts-service-scraper/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/badge/lint-ruff-000000)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2)](https://mypy-lang.org/)

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

# Resume that run: scrape metadata and download PDFs
uv run courts-scraper download --run-dir data/<timestamp>__supreme

# Or do both phases at once
uv run courts-scraper run --court supreme

# Check progress at any time
uv run courts-scraper status --run-dir data/<timestamp>__supreme
```

Useful options: `--delay` / `--jitter` (politeness spacing, defaults 5s + 2s),
`--max-pages` and `--limit` (sampling for testing), `--court` (repeatable).

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

### Cancel and resume

Press **Ctrl-C once** to stop cleanly. In-flight downloads are written to a
`.part` file and only atomically renamed to their final name once complete and
verified, so cancelling never leaves a half-file that a later run would mistake
for a finished download. Re-run the same `download` command to resume exactly
where you stopped.

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

If you use `courts-scraper` in research, please cite it. On GitHub, use the
**"Cite this repository"** button (top right, powered by
[`CITATION.cff`](CITATION.cff)) to get a formatted citation, or cite it directly:

> O'Brien, E. (2026). *courts-scraper: a research scraper for Courts Service of
> Ireland judgments* (Version 0.1.0) [Computer software].
> https://github.com/eoin-obrien/courts-service-scraper

To mint a permanent, versioned DOI, archive a release on
[Zenodo](https://zenodo.org/): enable the repository in Zenodo's GitHub
settings, publish a GitHub Release, and add the resulting DOI badge here and the
`doi:` field to `CITATION.cff`.

## License

Released under the [MIT License](LICENSE).
