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

### 5.1 Ingest notes (S1 findings)

Verified against the format contract and exercised by the committed fixtures
(`tests/fixtures/data/`) plus the generator (`tests/fixtures/gen.py`):

- **Columns.** `File Name, Size, Allocated, Modified, Attributes, Files, Folders`.
  Parsed **by column name**, case-insensitively, in any order; extra columns and
  missing optional columns are tolerated (`Allocated`/`Modified`/`Attributes`/
  `Files`/`Folders` are all optional; `File Name` and `Size` are required and
  their absence is a hard error). A duplicate mapped column takes the first
  occurrence; unknown columns are ignored.
- **Folder rows** end with a trailing backslash and carry *descendant totals*
  (`Size`/`Allocated`). They are indexed verbatim and never summed with their
  children: every byte total in `RunStats`/`db.index_summary()` comes from file
  rows only. `Files`/`Folders` cells are informational and not stored.
- **Storage normalization.** Folder paths are stored without the trailing
  separator (the marker survives as `is_dir`), except drive roots which keep it
  (`C:\` — a bare `C:` means "current directory on C:" on Windows). `name` is
  the last component (a drive root's name is `C:`), `depth` counts components
  below the root (root = 0), `ext` is the lower-cased extension without the dot
  (`''` when a file has none, `NULL` for folders). File rows keep their path
  exactly as exported.
- **Timestamps.** `Modified` is `yyyy/MM/dd HH:mm:ss`, zone-less local time.
  It is stored as an epoch under the documented assumption that the value is
  UTC, so ordering and age arithmetic are deterministic across machines (the
  index is not a timezone database; a machine's local zone can be re-applied by
  later slices if they need to). Empty/invalid cells become `NULL` and are
  counted (`bad_mtime_rows`).
- **Hard links.** Leading zero on a *non-zero* `Allocated` text value
  (`"01048576"`) marks a hard-linked file: the bytes are already accounted for
  by another entry. `allocated` keeps the parsed value and `hardlink_flag = 1`;
  roll-ups (`unique_allocated_bytes`) count the payload once, while the raw
  `allocated_bytes` total includes every copy. `"0"` is a genuine zero, not a
  marker; folder rows are never flagged.
- **Drive capacity rows — finding:** the seven-column export carries **no
  capacity data** and no capacity row is part of the contract. Ingest therefore
  treats summary rows as optional: a row named exactly like a drive spec
  without the trailing separator (`C:`) is never a tree entry and is counted as
  a capacity row; so is a `C:\` row whose `Modified`/`Attributes`/`Files`/
  `Folders` cells are *all* empty (only checked when those columns exist). Such
  rows are recorded in `drives` **only when they carry free-space data** (an
  explicit `Free`-style column, with capacity taken from a `Capacity`/`Total`
  column or the row's `Size`), otherwise they are skipped and reported in
  `RunStats.capacity_rows`. Consequence: **`drives` is usually empty after an
  import** — later slices must not assume capacity is known from the export and
  take free-space from the live system (plan targets, S5/S8).
- **Encoding.** UTF-8 (BOM tolerated, `utf-8-sig`) and UTF-16 (BOM'd or — via a
  NUL-heavy heuristic — BOM-less little/big endian). Detected once from a 4 KiB
  sample, then the file is streamed; the chosen encoding is recorded in `meta`
  (`ingest.encoding`).
- **Mechanics.** One transaction for the whole load, `executemany` batches of
  50 000 rows, `PRAGMA defer_foreign_keys=ON` during the load, `WAL` journal,
  parent chain reconstructed from a depth-first ancestor stack (an index lookup
  is the fallback for out-of-order rows). Memory stays flat regardless of the
  export size; measured ~24k rows/s (≈1.4M rows/min) in the dev container on a
  44 MB / 284k-row export, peak RSS 122 MB.
- **Tolerances (all counted, never fatal).** blank lines, short rows (missing
  cells become `""`/`NULL`), long rows (extra cells ignored), empty name cells,
  duplicate paths (`INSERT OR IGNORE`, counted), rows whose parent folder was
  never exported (`parent_id NULL`, `orphan_rows`), non-numeric size cells
  (stored as 0, `bad_number_rows`). Everything else fails loud: ingest refuses a
  non-empty index unless `--replace` is passed, and after every load the parsed
  counters are reconciled against the index (`IngestError` on mismatch).
- **Run provenance** lands in `meta` (`source.csv`, `source.csv_bytes`,
  `source.exported`, `source.machine`, `ingest.*` counters/bytes), giving later
  slices the dataset identity they need for caching and dataset locking.

### 5.2 Stats notes (S2 findings)

The `stats` stage (`spacesage/stats.py`) answers *where do the bytes live?* and
is the shared vocabulary later slices (rules, candidates, planner, GUI) build
on. Decisions that are now part of the data model:

- **Every byte total comes from file rows.** Folder rows are read *only* for
  the cross-check: each folder's verbatim `Size` is compared with the sum of
  its descendant file rows, and a disagreement beyond
  `max(4 KiB, 0.1 % of the exported total)` is reported as a
  `CrossCheckWarning` (the report shows them, the GUI can surface them, and a
  large spike means the export was taken while the disk was changing).
  Folder rows are never summed with their children.
- **Hardlink-aware totals.** `db.index_summary().unique_file_bytes` /
  `unique_allocated_bytes` credit a hard-linked group to its unflagged source;
  upstream (WizTree) marks only the *copies*, so an export whose every member
  is flagged would under-count (not observed in practice). `unique_*` values
  in the dir/extension/age/app views follow exactly the same rule.
- **Age buckets** are `<7d / 7-30d / 30-90d / 90-365d / >1y / unknown`
  (`mtime` at a caller-supplied `now`; future timestamps land in the youngest
  bucket, a missing timestamp in `unknown`). Age arithmetic uses the stored
  epoch, i.e. the documented "export timestamps are UTC" assumption.
- **Per-app heuristic.** Applications are the direct children of the folders
  ending in `AppData\Local`, `AppData\Roaming`, `Program Files` or
  `Program Files (x86)` (case-insensitive, component-wise, both separators).
  Footprints of the same name are merged across roots, users and spellings —
  `C:\Program Files\Chrome` + `C:\Users\a\AppData\Local\Chrome` +
  `c:\users\b\appdata\local\chrome` are one `Chrome`; the spelling with the
  most bytes names the row. Files directly under an app root are not
  attributed (they still count in the dir/ext/age views), and a folder
  without any app root yields no apps — the heuristic never guesses.
- **Materialised views.** Schema v2 holds `dir_sizes` (one row per folder,
  cascaded away with its entry) and `app_footprints` (name-keyed, roots as a
  JSON array), rebuilt by `stats.build_derived()` in one streaming pass. The
  report itself never reads them — it always recomputes from `entries`, so
  they can never drift into the numbers (the CLI writes them only with
  `--materialize`, keeping plain `spacesage stats` read-only).
- **Scale.** One streaming pass, memory bounded by the folder count plus the
  requested top-N; the 30 k-folder / 207 k-file synthetic export reports in
  ~1.3 s (vs ~11 s to ingest it).

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

The product is the GUI (PySide6 widgets). One window, left navigation, four areas:

1. **Import** — drag & drop or file picker for a WizTree CSV; parsing progress (rows/sec); target drive(s) + free-space reserve + size filters; "Analyze" never blocks the UI.
2. **Opportunities — the core screen.** One list of files and folders **sorted by potential gain, biggest wins first**. "Gain" = estimated bytes freed on the current drive by the suggested action (delete: full size; move: full size; compress: estimated savings; link/dedupe: recoverable duplicate bytes). Folder rows aggregate their descendants and selecting a folder cascades (no double counting).
   Every row carries a **suggested solution** — produced by the rule engine, with the AI system filling in and refining the rest: *Delete (quarantine)* / *Move to <drive>* / *Compress* / *Native tool* / *Link or dedupe* / *Review* — or an explicit ***No action*** with the reason when nothing can safely be done. Columns: select | path (mono) | size | est. gain | suggested solution + one-line why | tier | confidence. Filters: size / category / tier / state (has action / no action / undecided); search; bulk select; a compact summary strip (totals, drives, categories).
   A **details pane** shows the selected item: full reasoning, side effects, alternatives (delete vs. move vs. compress vs. native), move destination editor, and per-item AI actions ("Explain with AI", "Suggest with AI").
3. **Plan & Execute** — selected opportunities become a consolidated plan: per-item approve/reject; budget/conflict warnings; **Dry-run preview** (exactly what will happen); **Execute** behind an explicit confirmation dialog with live per-item progress (failures surfaced, never hidden); **Undo** view (journal history, one-click revert with verification).
4. **Settings** — thresholds, target drives, quarantine location, rule-pack overrides, AI providers (optional).

**No chat surface.** The AI lives inline where decisions happen: the suggested-solution column, the details pane (explanations, alternatives), plan review annotations, and one-click "apply as rule" — all rendered next to the items they concern. Exports the app can write on request: `plan.json` (the contract), `report.md` (shareable summary), and a static `report.html` *document* (convenience — not the app itself).

## 9.1 Design language (applies to every screen)

**Aesthetic:** clean, practical, modern. Information-dense but breathable; no gimmicks, no emoji in chrome; every control earns its place — a tool that respects the user's time.

- **Themes:** light + dark, following the OS by default, with a toggle in Settings. One token set; components never hardcode colors.
- **Tokens:** 4px spacing grid (4/8/12/16/24/32); 6–8px radii; subtle 1px borders; surface levels (base / raised / overlay); accent + semantic colors (success / warning / danger / info). Numeric columns right-aligned; paths and sizes in a mono stack; secondary text muted.
- **Typography:** system font stack (Segoe UI on Windows); scale ~11/12/13/15/18/24; headings tight, body comfortable.
- **Components:** metric cards for summaries; clean tables (no zebra striping; row hover; column sort; built-in search/filter); **tier badges** (T1/T2/T3) and confidence chips with consistent semantics; buttons — primary / secondary / danger (danger styling only for genuinely destructive actions); confirmation dialogs for anything irreversible; toasts for background results; real empty states (one-line explanation + primary action); progress with rows/sec and ETA where possible.
- **Icons:** bundled Lucide SVG subset (ISC license; attribution file included). No emoji in UI chrome.
- **Motion:** subtle only — 150–200 ms fades for panels/dialogs; nothing decorative; all engine work stays off the UI thread.
- **Accessibility:** full keyboard navigation with visible focus states; contrast-safe in both themes; scalable text; labels on every input and icon-only button.
- **AI surfaces:** inline where decisions happen — the **Suggested solution** column in the ranked list, a details pane with reasoning/alternatives and one-shot "Explain with AI", "Review plan" annotations, and "Apply as rule" on classifications. **No chat surface** — suggestions render next to the items they concern.

## 10. AI assist layer (optional) — see research doc

Verdict (details in [`research/ai-and-alternatives.md`](research/ai-and-alternatives.md)): **hybrid**, and the AI's headline job is the per-item **suggested solution**: for every entry the rules don't decide, the AI system proposes a course of action — or an explicit *No action* when nothing can safely be done. The list is ranked by gain, so the biggest wins surface first with their remedy attached.

**Capabilities (each bounded, schema-validated, dataset-locked):**

1. `suggest` — solution suggestions for list items, single or batched (action type from the engine's allowed vocabulary, "why", confidence, side effects, alternatives; supports *No action* / *Review* when uncertain). Results cached per entry; large lists fill in background batches.
2. `classify` — labels for ambiguous/matched-weakly entries; accepted results can be **promoted into user rule-pack entries**.
3. `explain` — deep, one-shot explanation for a selected item/selection (streams into the details pane).
4. `review` — scans a plan for overlooked risks; severity-tagged annotations attached to plan items.
5. `summarize` — optional plain-language summary of the plan.

**Engineering:** OpenAI-compatible `/chat/completions` (stdlib `urllib`) with **SSE streaming**; `/models` listing for the model picker; **multiple providers configured side-by-side** (name, base URL, key env var, model) with presets for ollama / lmstudio / openai / openrouter / custom; retries with backoff; actionable error taxonomy. No conversation store — the product is not conversational.

**Guardrails:** JSON-schema validation with one repair retry; dataset locking (every referenced path must exist in the index — hallucinated paths rejected); prompt-injection-resistant wrapping (filenames are data); response cache keyed by content hash; token/cost meter (visible in the UI when AI is on; batch runs show an estimate first); `redact_paths` mode; **local-only mode** (block non-loopback endpoints); off until configured; graceful degradation to deterministic output.

**UI surfaces** (docs §9): the suggested-solution column and details pane, "Generate AI suggestions" batch action with progress + cost estimate, inline "Explain with AI", "Review plan" annotations, provider management with "Test connection" (models + latency), cost meter in the status bar. AI output can never create an executable action by itself — it becomes suggestions, annotations, or rule proposals that a human accepts.

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
