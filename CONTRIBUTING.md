# Contributing

Thanks for your interest in improving `courts-scraper`. This is a research tool,
so correctness, clear documentation, and reproducibility matter more than speed
of feature delivery.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync --extra dev          # create the environment
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg --hook-type pre-push
```

## Checks

All of these run in CI and must pass before a change is merged:

```bash
uv run ruff format .         # format
uv run ruff check .          # lint
uv run mypy                  # type-check (strict)
uv run pytest                # test suite (no network required)
```

The test suite parses saved HTML fixtures and mocks HTTP, so it runs fully
offline. If you change parsing, add or update a fixture in `tests/fixtures/`
rather than depending on the live site.

## Commit messages

This project uses [Conventional Commits](https://www.conventionalcommits.org/),
enforced by a [commitizen](https://commitizen-tools.github.io/commitizen/)
`commit-msg` hook. The easiest way to write a compliant message is:

```bash
uv run cz commit
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`,
`chore`. Releases and the changelog are generated from these with `cz bump`.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; unrelated cleanups belong in their own PR.
3. Ensure the checks above pass locally (the pre-commit hooks do most of this).
4. Update `README.md` / docstrings if behaviour changes.

## Responsible use

This tool accesses a public government website. Keep the default politeness
settings (or make them gentler) for large crawls, and do not use it in ways that
would burden the service. See [SECURITY.md](SECURITY.md).
