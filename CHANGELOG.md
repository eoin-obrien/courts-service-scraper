# Changelog

Generated from [Conventional Commits](https://www.conventionalcommits.org/) with
[commitizen](https://commitizen-tools.github.io/commitizen/). Running `cz bump`
prepends each release's notes below and follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Feat

- add `--retry-skipped` to `fetch` (resume) and `update`, which re-queues rows an
  earlier pass skipped (e.g. a judgment not yet assigned a Neutral Citation) so a
  later run resolves them once the Courts Service backfills the metadata
- show a live progress dashboard for long-running crawls (ETA and finish time,
  current judgment, a countdown to the next polite request, and a loud banner when
  the site is down), with plain periodic status lines for piped/cron/narrow/`--quiet`
  runs; built on an event-driven progress reporter that keeps `rich` out of the
  crawl engine. The `--json` contract is unchanged.
- record listing completeness (`--max-pages` truncation) in the run manifest
- prompt for courts and confirm scrape scale before running
- implement courts.ie judgments scraper with resumable downloads
