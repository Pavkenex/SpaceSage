# SpaceSage

**Turn a [WizTree](https://www.diskanalyzer.com/) export into a complete, safety-gated course of action for a full disk.**

You already know *what* is big — you exported the tree with WizTree. SpaceSage turns that export into *what to do about it*, item by item:

- **Delete** — quarantine-first and undoable, with a plain-language rationale for every candidate.
- **Move** to another drive — cold media, archives, game libraries, dev caches; free-space aware.
- **Link back** — junctions / symlinks so apps keep working after their data moves.
- **Native fixes** — use the owning tool's own mechanism (Steam library move, WSL/Docker disk compact, DISM component cleanup, Storage Sense, …) instead of raw deletion.
- **Review** — everything ambiguous stays flagged and untouched until a human decides.

Every suggestion carries a risk tier, a confidence, an estimated space gain, and a "why". Nothing executes without an itemized, approved plan. Execution is dry-run by default; every action is journaled and undoable.

Optionally plug in **any OpenAI-compatible LLM** (cloud or local — Ollama, LM Studio, vLLM, OpenAI, OpenRouter, …) to help classify the ambiguous long tail, narrate the plan, or review it for risks. The AI can suggest; it can never execute.

It is a **desktop application** (PySide6/Qt) — a real windowed program, not a web app and not a CLI: import your export, review suggestions, approve a plan, execute it, undo if needed. It ships as a single portable executable (Windows, PyInstaller). Underneath, the analysis engine is a zero-dependency Python library (usable on its own), and a minimal internal CLI exists for development and automation.

## Status

🚧 In development. The full design and build plan live in [`docs/design.md`](docs/design.md); the AI-vs-deterministic research behind the hybrid architecture is in [`docs/research/ai-and-alternatives.md`](docs/research/ai-and-alternatives.md). Work is tracked as slices in [`docs/slices.md`](docs/slices.md).

## Docs

- [`docs/design.md`](docs/design.md) — architecture, data model, plan schema, safety model, executor design
- [`docs/slices.md`](docs/slices.md) — build slices with acceptance criteria
- [`docs/rules.md`](docs/rules.md) — rule-pack authoring guide (matchers, tiers, actions, ordering)
- [`docs/research/ai-and-alternatives.md`](docs/research/ai-and-alternatives.md) — research: LLM vs. rules vs. other systems
- [`docs/adr/`](docs/adr/) — architecture decision records

## Development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). The engine is stdlib-only; dev tooling lives in the `[dev]` extra (installed by default via `[tool.uv] default-extras`).

```sh
uv venv .venv
uv pip install -e '.[dev]'     # or simply `uv sync`

uv run pytest                  # tests (`-m 'not slow'` skips the slow ones)
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
uv run mypy spacesage          # types (strict)
```

The internal CLI (development/automation only) runs as `uv run python -m spacesage --version`, or via the installed `spacesage` console script. Four engine stages are available now:

```sh
uv run python -m spacesage ingest export.csv --db index.db   # --replace reloads, --progress reports to stderr
uv run python -m spacesage stats --db index.db                # --by dir|ext|age|app, --top N, --json, --materialize
uv run python -m spacesage classify --db index.db             # --rules DIR, --list-rules, --top N, --json, --materialize
uv run python -m spacesage candidates --db index.db           # --kind …, --min-size 100M, --top N, --json
uv run python -m spacesage plan --db index.db --to D: --reserve 20G -o plan.json   # --free SIZE, --no-links, --json
uv run python -m spacesage deepscan PATH... [--min-size 1M]   # --top N, --json, --progress (live filesystem, read-only)
```

`ingest` streams a WizTree export into the SQLite index; `stats` aggregates it (biggest directories and files, per-extension totals, age buckets, per-app footprints) from file rows only and reports folder-row disagreements as data-quality warnings. It is read-only unless `--materialize` is passed, which also rebuilds the derived `dir_sizes` / `app_footprints` tables for later stages.

`classify` runs the rule packs over every entry — built-in packs plus your own in `~/.config/spacesage/rules/` (`--rules DIR` to point elsewhere), shadowed by rule id — and prints per-tier / per-category counts and sizes plus the largest entries no rule recognised. It is read-only unless `--materialize` is passed, which writes the derived `categories` table (schema v3). Every rule carries a category, a risk tier, an action, a confidence and a plain-language rationale; see [`docs/rules.md`](docs/rules.md) to write your own.

`candidates` turns those verdicts into the ranked opportunities list — biggest estimated win first, per action kind: `delete` (quarantine candidates), `move` (data to relocate, grouped at directory level), `stale` (big, cold entries to review), `dupes-weak` (same name **and** size; explicitly flagged as unverified) and `app` (the largest application footprints, each carrying the advice of its biggest matching folder or an explicit "no action" with the reason). Every row carries its tier, confidence, the plain-language why and the four score factors (`bytes × tier weight × confidence × recency`), so a rank can be re-derived by hand. It is read-only, and folder rows aggregate their descendants — a candidate that another, higher-priority kind already claimed is not listed twice.

`plan` composes those candidates into the course of action (`plan.json`, schema `spacesage.plan/v1`): quarantines first (T1 before T2, biggest gain first), then moves onto the target drives you pick (`--to D:` per drive, each respecting `free_bytes − reserve`; `--free SIZE` states free space for drives this machine cannot measure), then compressions, then review and native-tool items. Directories move whole and get a junction back, files get a symlink flagged as needing elevation (or `--no-links` for no link at all), destinations mirror the source below `<target>\Moved`, and **T3 paths are never executable** — they stay review items. An app footprint whose advice is "delete the cache inside it" is a review item too; only what the rules actually told us to act on becomes an action. Byte totals never double count a folder and its children, and the whole document is deterministic: `plan_id` is a sha256 over the source and the action list, so re-planning the same index reproduces it exactly. It prints a Markdown summary (for reports and chat) and writes the JSON with `-o plan.json`. It is read-only.

`deepscan` is the optional live counterpart to the weak duplicate clusters: it walks the paths you name on *this* machine and proves which same-size files are byte-identical — group by size, hash the first 64 KiB, then read the full content only where it is still ambiguous. It never follows a symlink, junction or other reparse point, and it counts hard-linked paths as **one physical copy instead of free space** (deleting a hard link reclaims nothing). Every group gets the newest copy as the keeper (ties: the shortest path) and, when all copies sit on one volume, a suggestion to hardlink the rest back — the same bytes reclaimed while every path stays valid; across volumes it says so and the extra copies have to be deleted or moved. It prints the groups biggest win first (`--min-size 1M`, `--top N`, `--json`, `--progress`) and is strictly read-only: it reports, it never touches.

GUI dependencies arrive with the desktop-app slices — see [`docs/slices.md`](docs/slices.md) and [`docs/dev-environment.md`](docs/dev-environment.md).

## License

MIT. Not affiliated with WizTree / Antibody Software.
