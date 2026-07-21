# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). From the
first tagged release onward, new entries are generated from
[Conventional Commits](https://www.conventionalcommits.org/) with
[commitizen](https://commitizen-tools.github.io/commitizen/) (`cz bump`).

## [Unreleased]

## [0.1.0] - 2026-07-21

### Added

- Two-phase scraper for Courts Service of Ireland judgments: a listing phase
  that records the paginated search results into SQLite, and a download phase
  that scrapes per-judgment metadata and downloads the PDFs.
- Atomic, cancel-safe, checksum-verified downloads (`.part` temp file plus
  atomic rename) with resume support; cancelling never leaves a half-file that a
  later run accepts.
- Politeness controls (configurable delay, jitter, retry) for a public
  government server.
- Locale-independent Irish date parsing, PDF magic-byte verification, and
  filename construction from the Neutral Citation and authoring judge.
- Per-run, self-contained data folder (SQLite database, PDFs, durable error log,
  run manifest).
- Offline test suite covering parsing, dates, naming, resume logic, and the
  atomic-download guarantees.

[Unreleased]: https://github.com/eoin-obrien/courts-service-scraper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/eoin-obrien/courts-service-scraper/releases/tag/v0.1.0
