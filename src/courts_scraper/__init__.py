"""Research scraper for Courts Service of Ireland written judgments.

The package archives judgments published at ``ww2.courts.ie`` in two phases:

1. **Listing** -- iterate the paginated search results and record every result
   row (one row per judgment PDF) in a SQLite database.
2. **Download** -- for each recorded row, fetch the judgment's view page for
   authoritative metadata (Neutral Citation, Record Number, ...), then download
   the PDF into a per-run data folder with resume-safe, checksum-verified writes.

See :mod:`courts_scraper.cli` for the command-line entry points.
"""

__version__ = "0.1.0"
