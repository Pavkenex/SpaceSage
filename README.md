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

The internal CLI (development/automation only) runs as `uv run python -m spacesage --version`, or via the installed `spacesage` console script. GUI dependencies arrive with the desktop-app slices — see [`docs/slices.md`](docs/slices.md) and [`docs/dev-environment.md`](docs/dev-environment.md).

## License

MIT. Not affiliated with WizTree / Antibody Software.
