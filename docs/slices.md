# Build slices

Ordered, dependency-chained slices. Each slice is one kanban task on the `spacesage` board, and each must end with: tests green, CI green, committed to `main` in `/opt/data/spacesage`, and a result summary with evidence.

| # | Slice | Depends on |
|---|---|---|
| S0 | Scaffold: package layout, pyproject, ruff/mypy/pytest, GitHub Actions (linux+windows), smoke test, `spacesage --version` | — |
| S1 | Ingest: streaming WizTree CSV parser → SQLite (hardlink marker, folder/file rows, capacity rows, tolerant parsing) + fixtures + edge-case tests | S0 |
| S2 | Stats: dir sizes from file rows, per-extension/age/app aggregates, top-N, `stats` CLI | S1 |
| S3 | Rule packs + classifier: TOML format, matcher, 7 built-in packs, tiers, `classify` CLI, authoring docs | S2 |
| S4 | Candidates + scoring: delete/move/stale/dupe-cluster/app-footprint candidates, `candidates` CLI | S3 |
| S5 | Plan generator: plan.json v1, budgets for target drives, junction/symlink policy, golden tests, `plan` CLI | S4 |
| S6 | Deep scan: exact duplicate groups via size+hash on the live filesystem, same-volume hardlink dedupe suggestions, `deepscan` CLI | S5 |
| S7 | Executor + undo: dry-run/apply, quarantine, Windows (robocopy/mklink) + POSIX backends, journal, `undo`, re-validation, lock handling | S6 |
| S8 | Reports: self-contained interactive HTML (checklist → approved.json export) + Markdown, golden tests, `report` CLI | S7 |
| S9 | AI assist layer: OpenAI-compatible client, presets, schema validation, dataset locking, caching, cost meter, `ai check/summarize/review`, stub-server tests | S8 |
| S10 | CLI/config/packaging: config TOML + env, `config` command, PyInstaller recipe + release workflow, docs (quickstart, safety, FAQ) | S9 |
| S11 | E2E verification + v0.1.0: scenario generator → full pipeline incl. apply+undo in sandbox, all tests green, tag `v0.1.0` | S10 |
