# CLI redesign — the 10-star surface

Status: **design / approved**. Not yet implemented. Shaped in an office-hours
session and hardened by an `/autoplan` review (two independent outside voices),
2026-07-21. The three open calls are now resolved (see "Resolved decisions").
Tracked as GitHub issue #5. Target: a CLI that serves an unattended cron *and* a
hands-on academic equally, with free rein to rename (pre-1.0).

---

## The problem with today's surface

Nine flat commands, verbs and nouns mixed, with three concrete intuitiveness bugs:

1. **`list` is a trap.** It reads like "show me a list"; it actually starts a
   network crawl. The command that lists things is `runs`.
2. **`run` is overloaded** — the headline verb collides with the core noun
   ("a run" / `--run-dir` / `runs`).
3. **The two-phase architecture leaks.** `list` / `download` / `run` are three
   top-level commands that exist only because there are two internal phases.
   Users should never have to learn the phase model to fetch judgments.

Plus: no `--json` anywhere (bad for scripting), and every command re-plumbs
`--data-dir` / `--run-dir` / `--latest`.

The engine underneath (two-phase resumability, politeness gates, cancel handling,
provenance) is good and does not change. This is a naming-and-shape redesign.

---

## Design principles

- **Never make the user manage phases.** One resumable verb; "resume" = run it again.
- **Every interactive prompt has a flag twin; every human output has a `--json` twin.**
  This is what "serves cron and human equally" actually means, concretely.
- **The cost of a network action is always previewed and gated** before it runs
  (this already exists — preserve it). A friendly verb must never hide a
  full-corpus crawl.
- **Flat to type, grouped to read.** Commands stay flat; `--help` groups them
  into panels via Typer's `rich_help_panel`. Discoverability without nesting.

---

## The target surface

Global options live on the app callback, so no command repeats them:

| Global option | Default | Notes |
|---|---|---|
| `--data-dir` | `data`, env `COURTS_SCRAPER_DATA` | read once, shared by all commands. **Caveat: as a callback option it must precede the subcommand** — `courts-scraper --data-dir X runs`, not `courts-scraper runs --data-dir X` (the latter errors). Documented in `--help` and README because it fights muscle memory. |
| `--quiet` / `-q` | off | suppress Rich chrome/progress (honors `NO_COLOR` already) |
| `--version` | — | unchanged |

### Crawl (network, polite, resumable)

**`fetch`** — start or resume a scrape. Replaces `list`, `download`, and `run`.
```
courts-scraper fetch -c supreme -c high     # start a NEW run for these courts
courts-scraper fetch --latest               # resume the most recent run
courts-scraper fetch --run-dir data/2026... # resume a specific run
courts-scraper fetch                        # interactive: start new OR pick a run to resume
courts-scraper fetch -c supreme --list-only # listing phase only (rows, no PDFs) — was `list`
```
Dispatch keys on the **selector**, not on hidden disk state, so it is predictable:

| Invocation | Behavior |
|---|---|
| `-c/--court` | **start a new run** (old `run`): preview scale, confirm, list + download |
| `-c ... --list-only` | new run, **listing phase only** (old `list`) |
| `--run-dir X` / `--latest` | **resume** run X (old `download`) |
| `fetch` (no selector, TTY) | interactive picker: incomplete runs (newest-first) + "Start a new run" |
| `fetch` (no selector, non-TTY) | error, exit `2`: "pass `-c` to start a new run, or `--latest`/`--run-dir` to resume" |

Guards and rules (all three open-decision resolutions live here):
- `-c/--court` and `--run-dir`/`--latest` are **mutually exclusive** → `BadParameter` (exit `2`).
- `--list-only` requires `-c` (a new run); it errors with `--run-dir`/`--latest`, and rejects `--limit` (a downloads cap is meaningless with no downloads).
- **`fetch -c` guard against duplicate runs** (Decision 3, resolved → *new, guarded*):
  a matching **complete** run for those courts → refuse, point at `update` (kills the
  cron re-crawl trap); a matching **incomplete** run → TTY offers "resume that or start
  new?", non-TTY proceeds new but prints a stderr warning naming the run and how to
  resume it. Default intent stays "new"; resume stays explicit.
- **`--latest` means "newest run, any status"** everywhere (matches `status`/`export`/`update`).
  On `fetch` it resolves that run, then refuses *post-resolution* if it is complete —
  the selector semantics never change per-command.
- **Resume breadcrumb:** on every partial / cancelled / `--list-only` exit, print
  `Resume with: courts-scraper fetch --run-dir <dir>`. Collapsing `list` must not
  hide the path back in.

Flags: `-c/--court`, `--run-dir`, `--latest`, `--list-only`, `--limit`,
`--max-pages`, `--delay`, `--jitter`, `--max-attempts`, `--timeout`, `-y/--yes`,
`--user-agent`.

> **Listing-resume limitation (v1).** The engine tracks download progress, not a
> listing cursor, so an interrupted *listing* phase restarts on the next `fetch`
> (listing is treated as atomic); resume applies to the metadata+download phase.
> Documented so "resume = run it again" isn't oversold.
>
> **Listing completeness is now recorded (fast-follow, shipped).** After a listing
> pass finishes, the manifest gains a `listing` block —
> `{complete, truncated, max_pages, pages_fetched, pages_available}` — written
> atomically by `finalize_listing` *after* `run_listing` returns, so an interrupted
> listing leaves the block absent (honestly "not verified complete") rather than
> claiming a full crawl. `truncated` means the run covers fewer pages than the site
> currently advertises. Coverage is the largest contiguous prefix ever fetched
> (`max` of this pass and the prior recorded one, since listing only upserts), judged
> against *this* pass's `pages_available` — which the result set can outgrow between
> runs. So a prior full crawl does not stay "full" once the site grows and a capped
> `update` no longer reaches the new end; the verdict is recomputed, not trusted. The
> flag flows into the `fetch` duplicate-run guard, `runs`' summary/`--json`,
> single-run `export` descriptors, and the corpus `snapshot.json`, where per-run
> `listing.sources` is authoritative (no single corpus-level completeness boolean is
> claimed across differing court sets; `all_verified` and `any_truncated` are the
> only exact aggregates). This records the *fact* of incompleteness; actually
> resuming a truncated listing from a cursor is still the larger follow-up.

**`update`** — pull newly-published judgments into a complete run. Kept as its own
command *on purpose*: its cost profile (incremental, plus the loud `--revalidate`
full re-fetch) deserves its own name and its own confirmation gate rather than
hiding behind `fetch`/`sync`.
```
courts-scraper update --latest
courts-scraper update --run-dir ... --revalidate -y   # cron-safe
courts-scraper update --latest --json                 # machine-readable result summary
```
Flags: `--run-dir`, `--latest`, `--revalidate`, `--max-pages`, `--limit`,
politeness set, `-y/--yes`, `--user-agent`, `--json`. `update` gains a `--json`
result summary (`{new, revisions, errors, run}`) — the outside voices flagged it as
higher cron value than `runs --json`, since it's the actionable outcome a scheduled
job wants to branch on.

### Inspect (local, read-only)

**`status`** — one run's progress.
```
courts-scraper status --latest
courts-scraper status --run-dir ... --json   # counts dict as JSON
```

**`runs`** — every run under the data dir.
```
courts-scraper runs
courts-scraper runs --json                   # array of {name, courts, created, done, total, error, path}
```

### Publish (local, produces artifacts)

**`export`** — one run → Frictionless Data Package.
```
courts-scraper export --latest -f csv,json,parquet --out ./pkg
courts-scraper export --latest --json        # print the result manifest to stdout
```

**`corpus`** — all runs → citable BagIt bundle (dedup + fixity + datasheet).
```
courts-scraper corpus -f csv,json --out data/corpus
courts-scraper corpus --json
```

**`dictionary`** — the field data dictionary (was `data-dictionary`).
```
courts-scraper dictionary            # to stdout
courts-scraper dictionary --out docs/data-dictionary.md
```
Named `dictionary`, not `schema` (Decision 1, resolved): `schema` already names a
real machine artifact in this repo — the Frictionless Table Schema inside the
`datapackage.json` that `export` writes, which this dictionary is *generated from*.
`dictionary` matches the existing `DATA_DICTIONARY.md` and avoids handing prose to
someone who typed a word that promises a machine contract.

### `--help` panels (grouped, flat to type)

```
Crawl
  fetch    Start or resume a scrape (list + download).
  update   Pull newly-published judgments into a run.

Inspect
  status   Progress of a single run.
  runs     List all runs.

Publish
  export      One run -> Frictionless Data Package.
  corpus      All runs -> citable BagIt bundle.
  dictionary  Print the field data dictionary.
```
Plus a top-of-help "Typical flow" epilog:
```
  courts-scraper fetch -c supreme     # crawl + download
  courts-scraper status --latest      # check progress
  courts-scraper update --latest      # later: pull new judgments
  courts-scraper export --latest      # publish a data package
```

---

## Migration map (old → new)

| Old | New |
|---|---|
| `list` | `fetch --list-only` |
| `download` | `fetch` (resume — same verb) |
| `run` | `fetch` (with `-c`) |
| `update` | `update` (unchanged; gains nothing but stays) |
| `status` | `status` (+ `--json`) |
| `runs` | `runs` (+ `--json`) |
| `export` | `export` (+ `--json`) |
| `corpus` | `corpus` (+ `--json`) |
| `data-dictionary` | `dictionary` |

Break freely, no aliases (pre-1.0). The rename must sweep **runtime strings, not
just docs** — both outside voices found live user-facing references to old names:
`recovery.py:113` (`download --latest`), `export.py:198` + `docs/DATA_DICTIONARY.md`
(`data-dictionary`), `prompts.py` (`list`/`run`), `cli.py:233`/`:362` and the module
docstring. Sweep: `grep -rn -E '\b(list|download|run|data-dictionary)\b' src/ README.md docs/`
and fix every `courts-scraper <cmd>` invocation. A docs-only pass ships a CLI that
tells users to run deleted commands.

---

## Cross-cutting polish

- **Exit codes, documented (corrected after review — the first draft's table was
  wrong):**

  | Code | Meaning |
  |---|---|
  | `0` | success — including "run already complete" **and a clean first-Ctrl-C stop** (the cancel handler stops the run, prints status, returns 0) |
  | `1` | operational failure — site outage (clean "try later"), listing drift, `RunLocked`, uncaught engine error |
  | `2` | bad usage — `BadParameter`: mutually-exclusive selectors, empty `--user-agent`, missing manifest, non-TTY interactive `fetch`, empty `--format`. **Highly reachable in cron** (non-TTY without `--yes`/`-c`) — a cron branching "1 = retry" must not treat these as 1 |
  | `130` | interrupted — only on a **second** Ctrl-C (first is a clean 0) |
  | `143` | SIGTERM (how cron/systemd actually kill a job) — unhandled, documented so it isn't a surprise |

- **`--json` is data-only:** stdout carries *only* the JSON document; all
  diagnostics/progress go to stderr so the output parses as one value. No Rich markup
  (like today's `data-dictionary` uses plain `print`). `corpus`'s conflict / missing-PDF
  / unverified-version warnings become JSON fields, not side-prints.
- **Consistent run selector everywhere:** `--run-dir` | `--latest` |
  interactive picker — already consistent across `download`/`status`/`update`/
  `export`; keep it identical on `fetch`.
- **Short flags:** `-c` court, `-f` format, `-y` yes, `-q` quiet.

---

## Resolved decisions (office-hours + /autoplan outside voices)

All three were run past two independent reviewers (a Claude subagent and Codex, both
blind to each other). Consensus and final calls:

1. **Field-docs command → `dictionary`.** Both voices + primary read: unanimous.
   `schema` collides with the Frictionless Table Schema `export` already emits.
   *Resolved: `dictionary`.*
2. **`--data-dir` → global callback option (kept), plus `COURTS_SCRAPER_DATA` envvar.**
   Both voices recommended per-command+envvar (the global forces the flag to precede
   the subcommand, fighting muscle memory). **Owner override: keep it global** — the
   single-source-of-truth surface is worth the ordering caveat, which is documented
   loudly in `--help` and README. The env var is added either way for the cron case.
   *Resolved: global + envvar, caveat documented.*
3. **`fetch -c` on an existing run → start new, guarded.** Unanimous that `-c` = new
   (predictable dispatch); the guard (refuse on a matching *complete* run → `update`;
   warn/offer-resume on a matching *incomplete* run) closes the duplicate-run and
   cron-recrawl footguns without adding hidden-state magic. *Resolved: new + guard
   (see the `fetch` dispatch section).*

---

## Effort / risk

- **Effort:** M (grew from S/M after review). No engine changes. (a) renaming +
  runtime-string sweep, (b) merging `list`/`download`/`run` into one `fetch`
  entrypoint keyed on the selector, with the mutual-exclusion table and the
  duplicate-run guards, (c) `--json` on five commands (status/runs/export/corpus +
  update) with the stdout/stderr contract, (d) `--data-dir` on the callback + envvar,
  (e) exit-code normalization at the app boundary + documented table, (f) resume
  breadcrumb, (g) `rich_help_panel` + epilog + `fetch --help` mode examples, (h) doc
  rewrite.
- **Risk:** Low-Med. Riskiest piece is `fetch` dispatch; cover every branch with
  tests: new / new-list-only / resume-incomplete / complete-refuse-→update /
  incomplete-match-guard (TTY + non-TTY) / both-selectors-error / list-only+limit-error
  / list-only-then-resume. Second: exit-code normalization must not swallow real
  errors — assert the 0/1/2/130 mapping directly.

## The assignment

Before writing code: run `courts-scraper --help` and each subcommand's `--help`
as they exist today, and read them as a first-time user would. Write down every
spot where the help text tells you *what* but not *when* — those are the epilog
and panel descriptions you'll write. That five-minute read is what turns a rename
into a genuinely 10-star surface.
