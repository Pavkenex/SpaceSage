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

## Status

🚧 In development. The full design and build plan live in [`docs/design.md`](docs/design.md); the AI-vs-deterministic research behind the hybrid architecture is in [`docs/research/ai-and-alternatives.md`](docs/research/ai-and-alternatives.md). Work is tracked as slices in [`docs/slices.md`](docs/slices.md).

## Docs

- [`docs/design.md`](docs/design.md) — architecture, data model, plan schema, safety model, executor design
- [`docs/slices.md`](docs/slices.md) — build slices with acceptance criteria
- [`docs/research/ai-and-alternatives.md`](docs/research/ai-and-alternatives.md) — research: LLM vs. rules vs. other systems
- [`docs/adr/`](docs/adr/) — architecture decision records

## License

MIT. Not affiliated with WizTree / Antibody Software.
