# SpaceSage — Design & Build Plan

**Version:** 0.1 (draft, 2026-09-12) · **Status:** approved for build — slices in [`slices.md`](slices.md)

---

## 1. Problem

Drives fill up and it's never obvious *what to do*:

- WizTree tells you **what** is big, not **what to do** about it.
- Manual shell spelunking ("what is this 40 GB folder?") is slow and error-prone.
- Risky "cleaner" tools delete blindly; nothing plans **moves to another drive** and **links back** so apps keep working.

**Goal:** from a single WizTree CSV export (+ a few preferences), produce a complete, ordered, safety-gated **course of action** — every item with a tier, confidence, expected gain, and a plain-language "why" — then optionally execute it with full undo.

**Form factor:** a **desktop application** — a native windowed program built with PySide6 (Qt Widgets), shipped as a single portable `spacesage.exe` for Windows (PyInstaller). Not a web app and not a CLI: it runs locally, reads files, writes files, and only executes what you explicitly approve in the UI. The analysis engine is a zero-dependency Python library the UI wraps; a minimal internal CLI exists only for development, CI and automation — it is not the product surface.

## 2. Safety principles (non-negotiable)

1. **Analysis is read-only.** Execution requires an itemized, approved plan.
2. **Three risk tiers.** T1 = disposable temp/caches (quarantine after itemized approval); T2 = app-owned data, regenerable artifacts (investigate first, prefer native cleanup); T3 = report-only, never in an executable plan (system dirs, profile roots, repos, databases, unknown large files).
3. **"Delete" means quarantine.** Move to a quarantine store with a manifest; purge is a separate, later, explicit step. Undo = move back.
4. **Dry-run by default.** The app always shows a resolved dry-run preview; execution requires explicit confirmation of the current plan, and a plan_id-bound manifest gates exactly which items may run.
5. **Re-validate immediately before every operation** (exists, under expected root, not newly a reparse point, tier re-check).
6. **No wildcards, absolute paths only.** Refuse drive roots, profile roots, system dirs, unknown paths for destructive actions.
7. **Journal everything.** Undo in the app (or `spacesage undo <journal>` for automation) reverses moves and quarantines in reverse order, with verification.
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
  cli.py              # internal CLI (dev/tests/automation — NOT the product surface)
  config.py           # TOML config + env overrides
  db.py               # SQLite schema, migrations, helpers
  ingest.py           # streaming WizTree CSV → SQLite
  stats.py            # aggregates: dir/ext/age/app, top-N
  rules.py            # TOML rule packs, matcher, tiers
  candidates.py       # delete/move/stale/dupe/app candidate generation + scoring
  planner.py          # plan.json v1 generation (the course of action)
  deepscan.py         # optional live hash scan (exact duplicates)
  models.py           # dataclasses: Entry, Drive, Action, Plan, …
  report.py           # Markdown/HTML export documents (share-friendly, optional)
  executor/
    __init__.py       # platform dispatch
    journal.py        # JSONL journal + undo
    win.py            # robocopy / mklink / quarantine backend
    posix.py          # shutil / XDG-trash parity backend
  ai/
    client.py         # OpenAI-compatible /chat/completions (stdlib urllib)
    prompts.py        # use-case prompts + output JSON schemas
    guardrails.py     # schema validation, dataset locking, caching, cost meter
  app/                # ── THE PRODUCT: PySide6 desktop application ──
    __init__.py
    main.py           # QApplication bootstrap (python -m spacesage.app)
    windows.py        # main window, navigation, status bar
    views/            # import wizard, dashboard, suggestions, plan/execute, undo, settings
    models/           # QAbstractTableModel view-models over the engine
    assets/           # icons, theme resources
  rules/*.toml        # built-in rule packs
tests/
  fixtures/gen.py     # synthetic WizTree CSV generator (incl. "full disk" scenario)
  gui/                # pytest-qt offscreen GUI tests + screenshot artifacts
```

**Dependency policy:** the **engine** is zero-dependency (stdlib only) so it stays runnable everywhere, including headless machines. The desktop app adds one extra: `[gui]` = PySide6. Dev extras: pytest, pytest-qt, ruff, mypy, pyyaml; build: pyinstaller. The AI layer needs no SDK — plain HTTP via `urllib`.

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

## 9. Desktop app — screens & flows

The product is the GUI (PySide6 widgets). One window, left navigation, five screens:

1. **Import** — drag & drop or file picker for a WizTree CSV; parsing progress (rows/sec); auto-detected drives; target drive(s) + free-space reserve + size filters; "Analyze" never blocks the UI.
2. **Dashboard** — drive usage summary, category breakdown (bars / treemap), top directories & files (sortable, filterable), search.
3. **Suggestions** — candidates grouped by kind (Safe to delete / Move / Review); each row shows path, size, tier, confidence, "why"; editable destination for moves; bulk select; live total of selected gains.
4. **Plan & Execute** — the consolidated course of action; per-item approve/reject (backed by a plan_id-bound approved set); budget/conflict warnings; **Dry-run preview** (exactly what will happen); **Execute** behind an explicit confirmation dialog with live per-item progress; failures surfaced, never hidden; **Undo** view (journal history, one-click revert with verification).
5. **Settings** — thresholds, target drives, quarantine location, rule-pack overrides, AI configuration (optional).

AI assists (optional, off by default) surface inside these screens: "Explain this selection", "Review the plan" (adds annotations), "Ask about my disk" — advisory only, never executable.

Exports the app can write on request: `plan.json` (the contract), `report.md` (shareable summary), and a static `report.html` *document* (convenience — not the app itself).

## 9.1 Design language (applies to every screen)

**Aesthetic:** clean, practical, modern. Information-dense but breathable; no gimmicks, no emoji in chrome; every control earns its place — a tool that respects the user's time.

- **Themes:** light + dark, following the OS by default, with a toggle in Settings. One token set; components never hardcode colors.
- **Tokens:** 4px spacing grid (4/8/12/16/24/32); 6–8px radii; subtle 1px borders; surface levels (base / raised / overlay); accent + semantic colors (success / warning / danger / info). Numeric columns right-aligned; paths and sizes in a mono stack; secondary text muted.
- **Typography:** system font stack (Segoe UI on Windows); scale ~11/12/13/15/18/24; headings tight, body comfortable.
- **Components:** metric cards for summaries; clean tables (no zebra striping; row hover; column sort; built-in search/filter); **tier badges** (T1/T2/T3) and confidence chips with consistent semantics; buttons — primary / secondary / danger (danger styling only for genuinely destructive actions); confirmation dialogs for anything irreversible; toasts for background results; real empty states (one-line explanation + primary action); progress with rows/sec and ETA where possible.
- **Icons:** bundled Lucide SVG subset (ISC license; attribution file included). No emoji in UI chrome.
- **Motion:** subtle only — 150–200 ms fades for panels/dialogs; nothing decorative; all engine work stays off the UI thread.
- **Accessibility:** full keyboard navigation with visible focus states; contrast-safe in both themes; scalable text; labels on every input and icon-only button.
- **AI surfaces:** the assistant is a right-docked toggleable panel: streaming answers, markdown rendering, suggestion chips, clear/restart controls, provider/status line. It must feel like part of the app — not a bolted-on chat box.

## 10. AI assist layer (optional) — see research doc

Verdict (details in [`research/ai-and-alternatives.md`](research/ai-and-alternatives.md)): **hybrid**. The deterministic core makes every decision and computes all numbers; the AI layer is a **first-class part of the app experience — not a barebones add-on** — while staying unprivileged: it suggests, the engine verifies, the user approves, the executor acts.

**Capabilities (each bounded, schema-validated, dataset-locked):**

1. `classify` — label unmatched/ambiguous entries in bounded batches (category + confidence + evidence). Accepted results can be **promoted into user rule-pack entries** with one click.
2. `explain` — deep explanation for a selected item/selection: what it is, why it is safe or risky, what happens if acted on.
3. `summarize` — narrate the plan/report in plain language.
4. `review` — scan a plan for overlooked risks; results attach to plan items as severity-tagged annotations.
5. `ask` — conversational Q&A over pre-aggregated statistics (threads persisted; context bounded — never the raw export).

**Engineering:** OpenAI-compatible `/chat/completions` (stdlib `urllib`) with **SSE streaming**; `/models` listing for the model picker; **multiple providers configured side-by-side** (name, base URL, key env var, model) with presets for ollama / lmstudio / openai / openrouter / custom; retries with backoff; actionable error taxonomy; conversation threads persisted locally (bounded history; context assembled from aggregates + current selection + plan digest).

**Guardrails:** JSON-schema validation with one repair retry; dataset locking (every referenced path must exist in the index — hallucinated paths rejected); prompt-injection-resistant wrapping (filenames are data); response cache keyed by content hash; token/cost meter (visible in the UI when AI is on); `redact_paths` mode; **local-only mode** (block non-loopback endpoints); off until configured; graceful degradation to deterministic output.

**UI surfaces** (docs §9): docked assistant panel (streaming chat, markdown, suggestion chips), "Explain selection" on Suggestions/Plan, "Review plan" in the Plan view, provider management with "Test connection" (models + latency), cost meter in the status bar. AI output can never create an executable action by itself — it becomes annotations, suggestions, or rule proposals that a human accepts.

## 11. Internal CLI (development & automation only)

Not the product surface — a minimal CLI remains for development, CI and automation; every app flow has an engine-level equivalent:

```
spacesage ingest <csv> [--db DIR]              # load an export into the index
spacesage stats / classify / candidates        # engine stages
spacesage plan [--to D: --reserve 20G]         # plan.json v1
spacesage apply <plan.json> --approve <approved.json> [--execute]
spacesage undo <journal.jsonl>
spacesage ai check|summarize|review            # AI diagnostics
```

## 12. Config

`~/.config/spacesage/config.toml` (Windows: `%APPDATA%\spacesage\config.toml`), env overrides via `SPACESAGE_*`. API keys only via env or a user-owned config file (0600); never logged, never printed.

## 13. Testing

- Unit: CSV edge cases (BOM/UTF-16, quoted commas, extra columns, leading-zero hardlink marker, folder rows), matcher, scoring, planner goldens, schema validation, undo logic.
- Fixture generator: synthetic WizTree CSVs including a full "full disk" scenario and golden plan.
- Executor integration tests on temp trees: POSIX in every CI run; Windows job (`windows-latest`) for robocopy/mklink paths.
- AI tests against a local stub OpenAI-compatible server (stdlib `http.server`) — no network in tests.
- GUI: pytest-qt with `QT_QPA_PLATFORM=offscreen`; every key screen renders to a PNG artifact (eyeballed); all app logic sits in view-models with plain unit tests. Dev-container GL-lib setup: see `docs/dev-environment.md`.
- E2E: generate scenario → ingest → classify → plan → dry-run → apply in sandbox → undo → tree verified restored. Includes a GUI smoke pass (launch offscreen, screenshot each screen).

## 14. Packaging

The app ships as a **windowed single-file executable** (`spacesage.exe` — PyInstaller; no console window; app icon + version info). Windows build in the release workflow; Linux smoke build in CI so the spec cannot rot. The engine stays `pip install spacesage`-able for headless/scripting use (no GUI deps). MIT license. Semantic versioning + CHANGELOG.

## 15. Roadmap (post-v0.1)

- Near-duplicate images (pHash) and sampled-video similarity.
- Decision memory: learn accept/reject preferences locally (no LLM) and re-rank suggestions.
- Live-scan mode (skip WizTree) and export-diff mode ("what grew since last week?").
- Scheduled rescans with alerts ("C: dropped 20 GB this week").

## 16. Open questions

- Curated junction-compatibility notes per app class (MSIX/store apps, services, anti-cheat).
- NTFS compression guidance tuning (text/logs good; media bad).
- Reading WizTree MFT dump format (CSV only for v0.1).
