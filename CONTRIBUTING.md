# Contributing

Thanks for your interest. This is a generic database re-sync engine; it must stay generic and
**free of any real schema's identifiers**. Please read the leak-safety section before your first
commit.

## Development setup

With [mise](https://mise.jdx.dev):

```bash
mise install            # install the pinned Python
mise run install        # create .venv and install the package (editable) with dev deps
git config core.hooksPath .githooks   # enable the pre-commit / pre-push guards (once per clone)
```

Or plain venv:

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
git config core.hooksPath .githooks
```

## Everything is offline

The engine is developed and tested **without a database**. It generates set-based SQL that is
unit-tested by inspection; nothing here connects to Oracle.

```bash
mise run test          # or: python tests/test_engine.py   (also: pytest)
mise run print-sql     # render the generated SQL for the public sample
mise run guard         # leak guard (see below)
```

Tests run against the synthetic **Order/OrderLine** sample in `examples/`
(`sample_catalog.json` + `sample_resync.yaml`) — never against real data. When you add or change
behaviour, add or extend a test in `tests/test_engine.py`, and keep `print-sql` working for the
sample.

## Leak safety (mandatory)

The public repo must never contain a real schema's identifiers. Enforcement is layered:

- **`.denylist`** (gitignored) holds proprietary naming stems. `scripts/check-no-proprietary.sh`
  reads it. Contributors working against a real schema keep their own `.denylist`; a public clone
  has none, so the guard no-ops there.
- **Pre-commit / pre-push hooks** (`.githooks/`, enabled via `core.hooksPath`) run the guard and the
  tests, and the pre-push hook also scans the commits being pushed.
- **CI** runs the guard (from a repo secret), the tests, and a `print-sql` smoke test.

Real, environment-specific artifacts stay local and gitignored: `catalog.json`,
`config.skeleton.json`, `resync.yaml`, `INSTANCE.md`, `.denylist`. Don't commit them. Use the
Order/OrderLine sample for anything public.

## Architecture

`resync_engine/`:

- `model.py` — load the catalog (`config.skeleton.json`) and the config (`resync.yaml`).
- `graph.py` — topological sort; breaks nullable cycles, fails loud on non-nullable ones.
- `plan.py` — per-table plan: surrogate detection, FK-remap lineage, deferred columns.
- `sqlgen.py` — generate the set-based SQL (`MERGE`, id-map steps, delete-orphan, deferred).
- `runner.py` — dry-run counts, apply in a transaction, constraint handling.
- `seed.py` — `seed-config`: draft a `resync.yaml` from the catalog.
- `cli.py` — the `resync` command.

Design rationale lives in [`DESIGN.md`](DESIGN.md); the domain glossary in [`CONTEXT.md`](CONTEXT.md).
Keep both in sync when behaviour changes. Non-trivial, hard-to-reverse decisions get an ADR in
`docs/adr/`.

## Coding conventions

- Match the surrounding style; keep matching (`_expr`) and writing (`_write_expr`) separate in
  `sqlgen` — overrides and audit rules must never affect which rows pair up.
- Prefer small, testable pure functions (see `plan.fk_constraints`, `plan.unsequenced_surrogates`).
- SQL is generated, not hand-written per schema; new behaviour should be exercised by `print-sql`
  and an offline test.

## Pull requests

1. Branch from `main`.
2. Make the change with a test; run `mise run test` and `mise run guard` (both must pass — the
   hooks enforce this anyway).
3. Update `DESIGN.md` / `CONTEXT.md` if behaviour changed; add an ADR for a significant decision.
4. Open the PR against `main`. CI must be green.

## License

By contributing you agree that your contributions are licensed under the project's
**GNU AGPL-3.0-or-later** (see [`LICENSE`](LICENSE)).
