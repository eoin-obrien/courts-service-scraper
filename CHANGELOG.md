# Changelog

Generated from [Conventional Commits](https://www.conventionalcommits.org/) with
[commitizen](https://commitizen-tools.github.io/commitizen/). Running `cz bump`
prepends each release's notes below and follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fix

- select multiple courts in one run correctly. The multi-court query wrapped the
  per-court filters in an explicit `(A OR B)` group, which the courts.ie search
  parser does not understand: it dropped the whole court constraint and returned
  the entire unfiltered corpus (21,141 rows, including courts you never picked).
  Repeat the `filter:alfresco_Court.<name>` token joined by `AND` instead, which
  the site unions -- verified live (Supreme + High = 16,437, exactly those two)

### Feat

- add the six remaining courts the site exposes (`court_of_criminal_appeal`,
  `courts_martial_appeal`, `central_criminal`, `special_criminal`, `circuit`,
  `district`) alongside the existing three, plus group aliases `superior`
  (Supreme + Court of Appeal + High) and `all`, so `fetch --court superior`
  crawls every superior court in a single run
- add `corpus --archive <zip|tar|tar.gz|tar.bz2|tar.xz>`, which serialises the
  finished bag into a single shareable file (the archive's one top-level entry is
  the bag directory, per the BagIt serialisation convention, so it validates
  directly) and writes a `sha256sum`-compatible `.sha256` sidecar for verifying
  the transfer
- let `corpus` choose which runs to include: `--select` opens an interactive
  checklist (all pre-checked), or pass repeatable `--run-dir` folders; the default
  is still every readable run
- expand the derived-dataset controlled vocabularies from observed values: add
  `Unapproved` to the `Status` vocabulary, and match `Result` against a
  case/trailing-punctuation-normalised form seeded with the recurring canonical
  labels (so genuine free-text results still flag as drift, but standard outcomes
  no longer flood the flags -- ~98% -> ~12% flagged on the crawled corpus)
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
