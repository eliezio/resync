# Reproducing these artifacts with GitHub Copilot

This session produced two kinds of artifact. Reproduce them differently.

| Kind | Files | How to reproduce |
|------|-------|------------------|
| **Deterministic** (no model) | `extract-schema.sh`, `extract.jq`, `build-config.sh`, `resync_engine/`, `tests/`, `config.skeleton.json` | Copy the files and run them. Output is byte-stable given the same `catalog.json`. Do **not** ask Copilot to write these from scratch — version them. |
| **Judgment** (needs an LLM) | `DESIGN.md`, `CONTEXT.md`, `docs/adr/`, identity choices in `resync.yaml` | Drive Copilot with the pinned decisions below so it converges on the same result. |

The core trick: **put the design's "brain" into files Copilot loads automatically**, feed it the
*deterministic* schema facts (never the raw catalog), and let the model author only the judgment.

## 1. Deterministic pipeline (run as-is, no Copilot)

Prereqs: SchemaCrawler, `jq`, `tsort` (coreutils), Python 3.10+, `oracledb` + `PyYAML`.

```bash
# a. Extract the schema to catalog.json (needs DB access)
DB_HOST=… DB_PORT=1521 DB_SERVICE=… DB_SCHEMA=TARGET DB_USER=… DB_PASSWORD=… ./extract-schema.sh

# b. Flatten to config.skeleton.json + verify acyclic + print load order
./build-config.sh

# c. Engine: offline SQL preview + tests (no DB)
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tests/test_engine.py
.venv/bin/python -m resync_engine.cli print-sql --catalog config.skeleton.json --config resync.yaml
```

These reproduce the schema graph, load order, and generated SQL exactly. No AI variance.

## 2. Pin the decisions where Copilot will read them

Two files make Copilot behave like the agent in this session:

- **`.github/copilot-instructions.md`** — auto-loaded into every Copilot Chat request in the repo.
  Holds the invariants, locked decisions, and conventions. (Already in this repo.)
- **`.github/prompts/resync.prompt.md`** — a reusable prompt file. In VS Code, run it with the
  Chat "Run Prompt" action (or `/resync` once prompt files are enabled). (Already in this repo.)

Keeping `DESIGN.md` and `CONTEXT.md` in the repo matters: `@workspace` retrieves them, so the model
answers from the real spec instead of guessing.

## 3. Regenerate the judgment artifacts

Pick the mode closest to an agentic session:

### Option A — Copilot coding agent (closest match)
File an issue whose body is the contents of `.github/prompts/resync.prompt.md`, then assign it to
Copilot. It works in the background and opens a PR — the nearest equivalent to this session. Review
the PR against `DESIGN.md`.

### Option B — Copilot Chat, agent mode (VS Code)
Open Chat, select **Agent** mode, ensure `.github/copilot-instructions.md` is active, and paste:

> Follow `.github/prompts/resync.prompt.md`. Start at task 1 and stop after each task for my
> confirmation.

Attach context explicitly with `#file:config.skeleton.json`, `#file:DESIGN.md`, `#file:CONTEXT.md`.
Never attach `#file:catalog.json` — it is ~600 KB of nested JSON; the model will truncate or
hallucinate. All schema facts must come from the jq/tsort pipeline.

### Option C — reproduce the interview (the "grilling")
To recreate how the decisions were *derived* rather than replayed, start Chat with:

> Act as a skeptical reviewer. Interview me one question at a time to design a generic Oracle
> production→lower-environment re-sync that preserves target-created data. For each question give a
> recommended answer and wait for mine. Cover: what target changes to preserve, source deletes,
> where natural identity comes from, PII masking, cycles, execution topology, identity-less tables,
> owned-child deletes. Then write DESIGN.md, CONTEXT.md, and resync.yaml.

## 3b. Leak enforcement (mandatory before going public)

Proprietary schema identifiers must never enter the public repo. Two automated gates, plus a
denylist that itself stays private:

- **Denylist** — the real naming stems live in `./.denylist` (gitignored). Copy
  `scripts/denylist.example` → `.denylist` and fill it. `scripts/check-no-proprietary.sh` reads it;
  neither the script nor the example contains real stems.
- **Pre-commit hook** (primary gate — runs where `.denylist` exists):
  ```bash
  git config core.hooksPath .githooks   # enable once per clone
  ```
  Blocks any commit whose committable files match the denylist; also runs the offline tests.
- **CI** (`.github/workflows/ci.yml`) — runs the guard, tests, and a `print-sql` smoke test on
  every push/PR. Because `.denylist` is gitignored, CI rebuilds it from a repo **secret**:
  add `PROPRIETARY_DENYLIST` (Settings → Secrets → Actions) with the contents of your `.denylist`.
  On fork PRs the secret is unavailable, so the guard no-ops there — the pre-commit hook remains
  the authoritative local gate.

**Order of operations for a clean history:** genericize → `./scripts/check-no-proprietary.sh`
returns OK → `git init` → enable the hook → first commit → push. Never `git init` before the guard
passes, or a real identifier can persist in history even after later scrubbing.

## 4. Faithfulness gotchas

- **Schema facts → tools, never the model.** Any count, FK edge, load order, or column list must
  come from `jq`/`tsort`. Asking the model to read `catalog.json` is the main source of drift.
- **Determinism knobs.** Copilot has no temperature control; convergence comes from the pinned
  decision files, not sampling. Expect prose to differ; the *structure* and *decisions* should not.
- **Model differences.** Copilot backs onto different models than this session used. The locked
  decisions constrain outcomes, but review the generated `resync.yaml` identities and SQL by hand —
  they encode judgment the model can get subtly wrong (e.g. surrogate-lineage remap, owned-child
  scoping).
- **Verify, don't trust.** The offline tests (`tests/test_engine.py`) and `print-sql` are the
  objective check that a regenerated engine matches the intended behavior. Run them.
