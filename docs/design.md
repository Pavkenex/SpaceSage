# SpaceSage — Design & Build Plan

**Version:** 0.1 (draft, 2026-09-12) · **Status:** approved for build — slices in [`slices.md`](slices.md)

---

## 1. Problem

Drives fill up and it's never obvious *what to do*:

- WizTree tells you **what** is big, not **what to do** about it.
- Manual shell spelunking ("what is this 40 GB folder?") is slow and error-prone.
- Risky "cleaner" tools delete blindly; nothing plans **moves to another drive** and **links back** so apps keep working.

**Goal:** from a single WizTree CSV export (+ a few preferences), produce a complete, ordered, safety-gated **course of action** — every item with a tier, confidence, expected gain, and a plain-language "why" — then optionally execute it with full undo.

**Form factor:** a **local command-line program** — zero-dependency Python core, shipped as a single portable `spacesage.exe` for Windows (PyInstaller). No server, no web app, no accounts: it reads files, writes files, and only executes what you explicitly approve. The HTML report is a *static document* it generates; opening it is optional.

## 2. Safety principles (non-negotiable)

1. **Analysis is read-only.** Execution requires an itemized, approved plan.
2. **Three risk tiers.** T1 = disposable temp/caches (quarantine after itemized approval); T2 = app-owned data, regenerable artifacts (investigate first, prefer native cleanup); T3 = report-only, never in an executable plan (system dirs, profile roots, repos, databases, unknown large files).
3. **"Delete" means quarantine.** Move to a quarantine store with a manifest; purge is a separate, later, explicit step. Undo = move back.
4. **Dry-run by default.** `apply` prints resolved operations unless `--execute` is passed with an approved manifest; the manifest is bound to the plan by id.
5. **Re-validate immediately before every operation** (exists, under expected root, not newly a reparse point, tier re-check).
6. **No wildcards, absolute paths only.** Refuse drive roots, profile roots, system dirs, unknown paths for destructive actions.
7. **Journal everything.** `spacesage undo <journal>` reverses moves and quarantines in reverse order.
8. **No silent partial success.** Per-item results + summary; locked/in-use files skipped and reported.
9. **AI is optional and unprivileged.** AI can suggest, classify, narrate, review — it can never execute, and every AI-proposed item goes through the same validation pipeline as rule-based items.
10. **Machine scope.** An export from another machine is analysis-only unless local path existence is established.

## 3. Pipeline

```
WizTree CSV ─► ingest ─► SQLite index ─► stats ─► classify(rules) ─► candidates ─► plan ─► report ─► approve ─► execute ─► undo
                                                                                      ▲
                                                                        AI assist (optional, off by default)
```

## 4. Module layout

```
spacesage/
  __init__.py         # version
  cli.py              # argparse subcommands
  config.py           # TOML config + env overrides
  db.py               # SQLite schema, migrations, helpers
  ingest.py           # streaming WizTree CSV → SQLite
  stats.py            # aggregates: dir/ext/age/app, top-N
  rules.py            # TOML rule packs, matcher, tiers
  candidates.py       # delete/move/stale/dupe/app candidate generation + scoring
  planner.py          # plan.json v1 generation (the course of action)
  deepscan.py         # optional live hash scan (exact duplicates)
  models.py           # dataclasses: Entry, Drive, Action, Plan, …
  report.py           # self-contained HTML + Markdown reports
  executor/
    __init__.py       # platform dispatch
    journal.py        # JSONL journal + undo
    win.py            # robocopy / mklink / quarantine backend
    posix.py          # shutil / XDG-trash parity backend
  ai/
    client.py         # OpenAI-compatible /chat/completions (stdlib urllib)
    prompts.py        # use-case prompts + output JSON schemas
    guardrails.py     # schema validation, dataset locking, caching, cost meter
  rules/*.toml        # built-in rule packs
tests/
  fixtures/gen.py     # synthetic WizTree CSV generator (incl. "full disk" scenario)
```

**Dependency policy: zero-dependency core (stdlib only).** Optional extras: `[dev]` pytest/ruff/mypy, `[build]` pyinstaller. The AI layer needs no SDK — plain HTTP via `urllib`.

## 5. Data model (SQLite)

- `meta(key, value)` — source CSV, machine, export timestamp, schema version.
- `drives(name, fs_type, capacity_bytes, free_bytes)` — from capacity rows when present.
- `entries(id, path, name, parent_id, is_dir, size, allocated, mtime, attrs, hardlink_flag, depth, ext)` — both file and folder rows; parsed **by column name**; extra/new columns tolerated.
- Indexes on `size DESC`, `parent_id`, `mtime`, `ext`.
- Materialized derivations: `dir_sizes` (sums from **file rows only** — folder rows include children and double-count), `categories(entry_id, pack, rule_id, category, tier, confidence, rationale)`, `app_footprints(app, bytes, file_count)`.

**Scale:** exports run 1–20M rows. Streaming parse, batched inserts in a single transaction, `PRAGMA journal_mode=WAL`. Target ≥ 1M rows/min on a laptop; memory flat.

**Hard links:** a leading-zero `Allocated` value (WizTree marker) means the file consumes no additional space — index it, count it once in sums.

## 6. Rule packs (TOML)

TOML via stdlib `tomllib` (comments allowed, no extra dep). Built-ins in `spacesage/rules/`; user overrides in `~/.config/spacesage/rules/` shadow by `id`.

```toml
[[rule]]
id = "pip-cache"
path = ["**/AppData/Local/pip/Cache/**", "**/.cache/pip/**"]   # glob, case-insensitive on Windows
category = "dev-cache"
tier = "T1"
action = "DELETE_QUARANTINE"          # or MOVE / COMPRESS_NTFS / REVIEW / NATIVE
confidence = 0.95
rationale = "pip download/build cache; regenerates automatically; safe to clear when no builds are running."
native = "pip cache purge"            # optional: preferred native alternative shown in report
```

Optional matchers: `ext`, `min_size`, `older_than_days`, `name_regex`. First matching rule wins; unmatched entries → `unknown` (never destructive).

Built-in packs (v0.1): `windows.toml`, `dev.toml`, `browsers.toml`, `media.toml`, `games.toml`, `installers.toml`, `misc.toml` — see slices for content scope.

## 7. Plan schema (plan.json v1) — the contract

```json
{
  "schema": "spacesage.plan/v1",
  "plan_id": "sha256:…",
  "created": "2026-09-12T00:00:00Z",
  "source": {"csv": "…", "machine": "…", "exported": "…"},
  "targets": {"D:": {"free_bytes": 0, "reserve_bytes": 0}},
  "summary": {"delete_bytes": 0, "move_bytes": 0, "review_bytes": 0},
  "actions": [
    {"id": "a1", "type": "DELETE_QUARANTINE", "path": "C:\\…\\Temp", "bytes": 0,
     "category": "windows-temp", "tier": "T1", "confidence": 0.95,
     "rationale": "…", "side_effects": "…", "native_alt": null},
    {"id": "a2", "type": "MOVE", "path": "C:\\…", "dest": "D:\\Moved\\…",
     "link_after": "JUNCTION", "bytes": 0, "tier": "T2", "confidence": 0.8, "rationale": "…"},
    {"id": "a3", "type": "COMPRESS_NTFS", "path": "…", "bytes": 0, "rationale": "…"},
    {"id": "a4", "type": "REVIEW", "path": "…", "bytes": 0, "rationale": "…"},
    {"id": "a5", "type": "NATIVE", "command": "wsl --manage <distro> --set-sparse true", "why": "…"}
  ]
}
```

- **Approval manifest** (`approved.json`) = the subset of action ids a human approved. Execution refuses if the manifest's `plan_id` doesn't match the plan or if the action list changed.
- Action types v0.1: `DELETE_QUARANTINE`, `MOVE` (+`link_after`: `JUNCTION|SYMLINK|HARDLINK|NONE`), `COMPRESS_NTFS`, `REVIEW`, `NATIVE`.
- **Move planning:** candidates matched to target drives the user picked, respecting `free_bytes − reserve_bytes` budgets. Directories get `JUNCTION` (no admin needed); files get `SYMLINK` (needs elevation or Developer Mode — flagged); `HARDLINK` only same-volume, never for cross-drive moves.
- Ordering: T1 quarantine × gain desc, then moves, then compressions, then review/native.

## 8. Executor

- `spacesage apply <plan.json> --approve <approved.json> [--execute]`.
- **Windows backend:** moves via `robocopy /MOVE /E /COPYALL` (ACL-preserving) with verify (size + count), `shutil.move` fallback; junction `mklink /J` (no admin) via `cmd /c`; symlink flagged when elevation is required (detect `SeCreateSymbolicLinkPrivilege` / Developer Mode); quarantine = same-volume `_spacesage_quarantine/<plan_id>/…` + manifest; locked files: attempt exclusive open, skip + report.
- **POSIX backend** (dev/tests): `shutil.move`, `os.symlink`, XDG-trash-style quarantine for parity.
- **Journal:** append-only JSONL (`journal.jsonl`): op, before, after, result, verification. `spacesage undo <journal>` reverses in reverse order and verifies.
- **Long paths:** `\\?\`-prefixing on Windows; `MAX_PATH` handling tests.
- **Re-validation** right before every op, and again after (result verification).

## 9. Reports & approval

SpaceSage is a local program: there is **no server and no web app**. Its human-facing output is **documents**, and approval is a **program flow first**:

- `report.md` — full report in Markdown (terminal-friendly, chat-friendly).
- `report.html` — a **static, self-contained file** (inline CSS/JS, no CDN, no server): summary cards (total size, per-drive free, estimated gains), category bars, sortable/filterable tables (top dirs, top files, candidates, duplicate groups), and the action list as a visual checklist. Opening it is optional; it can also export the same `approved.json`.
- `spacesage approve <plan.json> --select a1,a3 | --interactive | --tier T1` — the **primary approval path**: writes `approved.json` (itemized, plan_id-bound). `--interactive` is a terminal checklist.
- `spacesage report --format html|md|json`.

## 10. AI assist layer (optional) — see research doc

Verdict (details in [`research/ai-and-alternatives.md`](research/ai-and-alternatives.md)): **hybrid**. The deterministic core makes every decision and computes all numbers. The AI layer adds four bounded, unprivileged capabilities:

1. `classify` — label unmatched/ambiguous entries in bounded batches (output: category + confidence + rationale, JSON-schema-validated, dataset-locked).
2. `summarize` — narrate the plan/report in plain language.
3. `review` — scan a plan for overlooked risks (adds REVIEW annotations only).
4. `ask` — Q&A over pre-aggregated statistics (never the raw export).

Config: `base_url`, `api_key`, `model`, `timeout`, `max_tokens`, `redact_paths`; presets for `ollama` (http://localhost:11434/v1), `lmstudio`, `openai`, `openrouter`, custom. Guardrails: schema validation with repair retry, every referenced path must exist in the index, AI output enters the same approval pipeline as everything else, response caching keyed by content hash, token/cost meter in reports, **off until configured**, graceful degradation to deterministic output. Filenames are treated as untrusted data (prompt-injection resistant prompts).

## 11. CLI surface

```
spacesage ingest <csv> [--db DIR]              # stream + index
spacesage stats [--top N] [--by ext|age|app|dir]
spacesage classify                             # run rule packs → categories
spacesage candidates [--kind delete|move|stale|dupes|app]
spacesage deepscan <root>… [--yes]             # optional live hash scan (exact dupes)
spacesage plan [--to D: --reserve 20G]         # → plan.json + summary
spacesage approve <plan.json> [--select a1,a3 | --interactive | --tier T1]   # → approved.json (primary)
spacesage report [--format html|md|json]       # static document, optional
spacesage apply <plan.json> --approve <approved.json> [--execute]
spacesage undo <journal.jsonl>
spacesage ai check | ai summarize | ai review
spacesage config set|get|list
```

## 12. Config

`~/.config/spacesage/config.toml` (Windows: `%APPDATA%\spacesage\config.toml`), env overrides via `SPACESAGE_*`. API keys only via env or a user-owned config file (0600); never logged, never printed.

## 13. Testing

- Unit: CSV edge cases (BOM/UTF-16, quoted commas, extra columns, leading-zero hardlink marker, folder rows), matcher, scoring, planner goldens, schema validation, undo logic.
- Fixture generator: synthetic WizTree CSVs including a full "full disk" scenario and golden plan.
- Executor integration tests on temp trees: POSIX in every CI run; Windows job (`windows-latest`) for robocopy/mklink paths.
- AI tests against a local stub OpenAI-compatible server (stdlib `http.server`) — no network in tests.
- E2E: generate scenario → ingest → classify → plan → dry-run → apply in sandbox → undo → tree verified restored.

## 14. Packaging

Zero-dep core → `pipx install spacesage` or `python -m spacesage`. Windows single-file `spacesage.exe` via PyInstaller in the release workflow. MIT license. Semantic versioning + CHANGELOG.

## 15. Roadmap (post-v0.1)

- Near-duplicate images (pHash) and sampled-video similarity.
- Decision memory: learn accept/reject preferences locally (no LLM) and re-rank suggestions.
- Live-scan mode (skip WizTree) and export-diff mode ("what grew since last week?").
- Scheduled rescans with alerts ("C: dropped 20 GB this week").

## 16. Open questions

- Curated junction-compatibility notes per app class (MSIX/store apps, services, anti-cheat).
- NTFS compression guidance tuning (text/logs good; media bad).
- Reading WizTree MFT dump format (CSV only for v0.1).
