# Security Policy

## Reporting a vulnerability

If you discover a security issue in `courts-scraper` (for example, a way the
download or file-naming logic could be abused to write outside the data folder),
please report it privately rather than opening a public issue.

- Email: **eoinobrien910@gmail.com**
- Please include steps to reproduce and, if possible, a minimal example.

You can expect an acknowledgement within a few days. Once a fix is available it
will be released and credited (unless you prefer to remain anonymous).

## Supported versions

This is an actively developed research tool; only the latest released version is
supported. Fixes are shipped on `main` and in the next tagged release.

## Responsible use

`courts-scraper` accesses a public government website
([ww2.courts.ie](https://ww2.courts.ie)). It is intended for research and
archival use of public court judgments. Please:

- Keep the default politeness settings (single worker, multi-second delay) or
  make them gentler for large crawls.
- Do not run it in a way that would degrade the service for others.

The maintainers are not responsible for misuse. Downloaded judgments are public
records; their reuse is subject to the Courts Service of Ireland's terms and
applicable law. See the **Disclaimer and responsible use** section of the
[README](README.md#disclaimer-and-responsible-use) for the full statement
(no affiliation, personal-data obligations, accuracy, and no warranty).
