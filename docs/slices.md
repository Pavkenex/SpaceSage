# Build slices

Ordered, dependency-chained slices. Each slice is one kanban task on the `spacesage` board, and each must end with: tests green, CI green, committed to `main` in `/opt/data/spacesage`, and a result summary with evidence.

**S0–S7 build the engine** (UI-agnostic library + internal dev CLI). **S8–S12 build the product surface** (PySide6 desktop app) and ship it.

| # | Slice | Depends on |
|---|---|---|
| S0 | Scaffold: package layout, pyproject, ruff/mypy/pytest, GitHub Actions (linux+windows), smoke test | — |
| S1 | Ingest: streaming WizTree CSV parser → SQLite + fixtures + edge-case tests | S0 |
| S2 | Stats: dir/ext/age/app aggregates, top-N | S1 |
| S3 | Rule packs + classifier: TOML packs, matcher, tiers | S2 |
| S4 | Opportunities & scoring: ranked items (biggest gains first), each with a suggested solution (or explicit No action); delete/move/stale/dupe/app | S3 |
| S5 | Plan generator: plan.json v1, budgets, link policy, goldens | S4 |
| S6 | Deep scan: exact duplicate groups (live filesystem, hash-based) | S5 |
| S7 | Executor + undo: quarantine, robocopy/mklink, journal, undo | S6 |
| S8 | GUI shell + core screen: PySide6 window, import wizard, **ranked Opportunities list + details pane**, design tokens + light/dark theme | S7 |
| S9 | Plan & execute & undo views (built from selected opportunities) + polish pass | S8 |
| S10 | AI engine: streaming multi-provider client; suggest / classify / explain / review use cases; guardrails; cache; cost meter | S9 |
| S10b | AI suggestions in the list: batch fill, per-item suggest & explain, apply-as-rule, plan review annotations, provider settings (UI) | S10 |
| S11 | Packaging & docs: windowed single-file exe, app guide with real screenshots | S10b |
| S12 | E2E verification + v0.1.0 (engine pipeline + GUI smoke pass) | S11 |
