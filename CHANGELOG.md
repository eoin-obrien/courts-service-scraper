# Changelog

Generated from [Conventional Commits](https://www.conventionalcommits.org/) with
[commitizen](https://commitizen-tools.github.io/commitizen/). Running `cz bump`
prepends each release's notes below and follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Feat

- show a live progress dashboard for long-running crawls (ETA and finish time,
  current judgment, a countdown to the next polite request, and a loud banner when
  the site is down), with plain periodic status lines for piped/cron/narrow/`--quiet`
  runs; built on an event-driven progress reporter that keeps `rich` out of the
  crawl engine. The `--json` contract is unchanged.
- record listing completeness (`--max-pages` truncation) in the run manifest
- prompt for courts and confirm scrape scale before running
- implement courts.ie judgments scraper with resumable downloads
