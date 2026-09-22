# SpaceSage

![SpaceSage — the Opportunities screen: ranked cleanup suggestions with a tier, a confidence and the reasoning for every row](artifacts/gui/opportunities.png)

**Turn a [WizTree](https://www.diskanalyzer.com/) export into a complete, safety-gated course of action for a full disk.**

You already know *what* is big — you exported the tree with WizTree. SpaceSage is the desktop app that turns that export into *what to do about it*, item by item:

- **Delete** — quarantine-first and undoable, with a plain-language rationale for every candidate.
- **Move** to another drive — cold media, archives, game libraries, dev caches; free-space aware, with a link left behind so apps keep working.
- **Link back** — junctions / symlinks so a program still finds its data after the folder moved.
- **Native fixes** — use the owning tool's own mechanism (Steam library move, WSL/Docker disk compact, DISM component cleanup, Storage Sense, …) instead of raw deletion.
- **Review** — everything ambiguous stays flagged and untouched until a human decides.

Every suggestion carries a risk tier, a confidence, an estimated space gain and a "why". Nothing executes without an itemized, approved plan: analysis is read-only, execution is dry-run first, every operation is journaled, and everything can be undone. That contract is written down in [`docs/safety.md`](docs/safety.md).

Optionally plug in **any OpenAI-compatible LLM** (cloud or local — Ollama, LM Studio, vLLM, OpenAI, OpenRouter, …) to help classify the ambiguous long tail, narrate a row, or review a plan for risks. The AI can suggest; it can never execute.

It is a **desktop application** (PySide6/Qt) — a real windowed program, not a web app and not a CLI. It ships as a single portable executable for Windows (PyInstaller; no console window, app icon and version resource) and as a Linux bundle. Underneath, the analysis engine is a **zero-dependency Python library** usable on its own (see [Engine](#engine-library--cli) below).

## Quick start

1. **Get the app** — `spacesage.exe` from this project's releases page (Windows), the Linux bundle, or from source:
   ```sh
   uv venv .venv && uv pip install -e '.[dev]'    # or: uv sync
   uv run python -m spacesage.app                 # the window (also: the `spacesage-app` script)
   ```
2. **Export your tree** in WizTree (*Export to CSV* — its feature, not this app's).
3. **Import it** in SpaceSage, pick the target drive and the free-space reserve, press *Analyze*.
4. **Review the ranked list**, check what you agree with, **Build plan**.
5. **Dry-run preview** resolves every approved action to exactly what would happen; **Execute** sits behind one itemized confirmation.
6. **Undo** is the switch beside *Plan*: every operation is listed, verified by digest and revertible.

The full walkthrough is [`docs/quickstart.md`](docs/quickstart.md); the screen-by-screen reference is [`docs/app-guide.md`](docs/app-guide.md).

## The app in pictures

| | |
|---|---|
| ![The Import screen](artifacts/gui/import.png) | ![The dry run: every approved action resolved](artifacts/gui/dryrun.png) |
| **Import** — drop a WizTree CSV, set target drive / reserve / size floor, analyze on a worker thread. | **Dry run** — the exact operations a run would perform, and nothing on disk is touched. |
| ![AI suggestions filled into the list](artifacts/gui/suggestions_filled.png) | ![The Undo screen with journals and verification](artifacts/gui/undo.png) |
| **AI, optionally** — batch-fill suggestions for the undecided rows, explain a row, review a plan; the AI advises, it never executes. | **Undo** — journals, one row per operation, verified against the digest recorded on the way in. |

Every picture above is a real render produced by the GUI test suite and committed under [`artifacts/gui/`](artifacts/gui) — if a screen changes, its evidence changes with it.

## Status

**v0.1.0** — the full build (S0–S12) is done: the engine, the desktop app, the optional AI layer, the packaged build and the end-to-end acceptance pass. The full design and build plan live in [`docs/design.md`](docs/design.md); the AI-vs-deterministic research behind the hybrid architecture is in [`docs/research/ai-and-alternatives.md`](docs/research/ai-and-alternatives.md). Work is tracked as slices in [`docs/slices.md`](docs/slices.md); what "done" was proven to mean is in [`docs/verification.md`](docs/verification.md).

## Docs

| doc | what is in it |
|---|---|
| [`docs/quickstart.md`](docs/quickstart.md) | install → import → review → plan → execute → undo |
| [`docs/app-guide.md`](docs/app-guide.md) | every screen, every control, with real screenshots |
| [`docs/safety.md`](docs/safety.md) | the tiers, the guardrails, what never happens without confirmation |
| [`docs/faq.md`](docs/faq.md) | the questions people actually ask (including "does it delete my files?") |
| [`docs/ai.md`](docs/ai.md) | the optional AI layer: providers, privacy switches, cost control, guardrails |
| [`docs/design.md`](docs/design.md) | architecture, data model, plan schema, safety model, executor design |
| [`docs/rules.md`](docs/rules.md) | rule-pack authoring guide (matchers, tiers, actions, ordering) |
| [`docs/slices.md`](docs/slices.md) | build slices with acceptance criteria |
| [`docs/verification.md`](docs/verification.md) | the acceptance pass, the evidence it leaves, and where each slice's feature lives |
| [`docs/dev-environment.md`](docs/dev-environment.md) | the dev container (vendored GL libs, offscreen Qt) |
| [`docs/adr/`](docs/adr/) | architecture decision records |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed, release by release |

## Engine (library & CLI)

The engine is the part that reads exports, classifies entries, ranks
opportunities, plans and executes — UI-agnostic and stdlib-only by design, so it
runs on headless machines and inside scripts. The app wraps it; a minimal
internal CLI exposes every stage for development, CI and automation.

```sh
uv run python -m spacesage --version
uv run python -m spacesage ingest export.csv --db index.db   # --replace reloads, --progress reports to stderr
uv run python -m spacesage stats --db index.db                # --by dir|ext|age|app, --top N, --json
uv run python -m spacesage classify --db index.db             # --rules DIR, --list-rules, --top N, --now WHEN, --json
uv run python -m spacesage candidates --db index.db           # --kind …, --min-size 100M, --top N, --now WHEN, --json
uv run python -m spacesage plan --db index.db --to D: --reserve 20G -o plan.json
uv run python -m spacesage deepscan PATH... [--min-size 1M]   # live filesystem, read-only, hashes proofs
uv run python -m spacesage apply plan.json --approve approved.json            # dry run: resolves, touches nothing
uv run python -m spacesage apply plan.json --approve approved.json --execute  # quarantines/moves/links, journaling
uv run python -m spacesage undo spacesage.journal.jsonl                       # reverses them, newest first, verifying
```

Notes that matter if you script it:

- **`ingest` → `stats` → `classify` → `candidates` → `plan` are read-only**
  (`--materialize` is the explicit exception: it rebuilds the derived tables).
  `apply` is the only verb that touches the disk, and only what an approval
  manifest names.
- **The approval manifest is the gate**: `{schema, plan_id, approved: [a1, …]}`
  bound to that plan's own `plan_id`. A manifest for another plan, or one naming
  an id the plan does not have, is refused before anything is resolved.
- **`apply` without `--execute` is the dry run** and prints exactly what would
  happen; `--execute` re-validates every item against the live filesystem first
  and skips-with-a-reason anything that does not hold.
- **The plan document is deterministic**: `plan_id` is a sha256 over the source
  and the action list, so re-planning the same index reproduces it exactly.
- **`deepscan`** is the verified-duplicate counterpart of the list's weak-dupe
  clusters: group by size, hash the first 64 KiB, read the full content only
  where it is still ambiguous, count hard links as one physical copy, and never
  follow a reparse point.

The engine is installable on its own (`pip install spacesage`, no GUI
dependencies) and the library entry points are `spacesage.ingest`,
`spacesage.stats`, `spacesage.rules`, `spacesage.candidates`,
`spacesage.planner`, `spacesage.deepscan` and `spacesage.executor`. The details —
schema, scoring, plan format, executor semantics — are in
[`docs/design.md`](docs/design.md) §3–§8.

## Development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). The engine is
stdlib-only; dev tooling lives in the `[dev]` extra, PyInstaller in `[build]`.

```sh
uv venv .venv
uv pip install -e '.[dev]'     # or simply `uv sync`

uv run pytest                  # tests (`-m 'not slow'` skips the slow ones)
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
uv run mypy spacesage          # types (strict)
```

GUI tests are pytest-qt on Qt's offscreen platform (no display needed); the
screenshot renders land in [`artifacts/gui/`](artifacts/gui) and are asserted to
be real paints, not blank frames. In the dev container, source the helper first
(`source scripts/gui-env.sh` — vendored GL libs + `QT_QPA_PLATFORM=offscreen`,
see [`docs/dev-environment.md`](docs/dev-environment.md)).

The acceptance pass plants a full disk of its own and drives it through the
whole pipeline and the app (`uv run pytest tests/e2e`, a minute and about a
gigabyte of scratch); its renders, the demo plan and the run logs land in
[`artifacts/e2e/`](artifacts/e2e). What it proves, test by test:
[`docs/verification.md`](docs/verification.md).

### Packaging

```sh
uv run --extra build pyinstaller packaging/spacesage.spec --noconfirm \
    --distpath dist --workpath build/pyinstaller     # -> dist/spacesage[.exe]
./dist/spacesage --self-check                        # packaged app reports its Qt platform
QT_QPA_PLATFORM=offscreen ./dist/spacesage --capture artifacts/package/smoke.png
```

The spec ([`packaging/spacesage.spec`](packaging/spacesage.spec)) builds a
**one-file windowed** app — no console window, app icon from
[`spacesage/app/assets/app-icon.svg`](spacesage/app/assets/app-icon.svg), and a
Windows version resource generated from `spacesage.__version__`. CI builds and
*runs* that bundle on every push (the `package` job, with the smoke render as an
artifact); pushing a `v*` tag runs the release workflow, which builds the Windows
exe and the Linux bundle, smoke-tests both and attaches them to the release. The
render the Linux bundle produced for itself is committed at
[`artifacts/package/packaged-linux.png`](artifacts/package/packaged-linux.png).

## License

MIT. Not affiliated with WizTree / Antibody Software.
