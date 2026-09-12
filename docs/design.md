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
                                                                                      ▲
                                                 deep scan (optional): hashes the live filesystem, not the export
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
  opportunities.py    # the ranked list the GUI shows: rows, filters, folder cascade, summary
  planner.py          # plan.json v1 generation (the course of action)
  deepscan.py         # optional live hash scan (exact duplicates)
  models.py           # dataclasses: Entry, Drive, Action, Plan, …
  report.py           # Markdown/HTML export documents (share-friendly, optional)
  executor/
    __init__.py       # dispatcher: manifest, resolution, re-validation, apply
    backend.py        # digests, guards, the platform protocol
    report.py         # apply/undo result types and their rendering
    journal.py        # JSONL journal + undo
    win.py            # robocopy / mklink / compact backend
    posix.py          # shutil / symlink / XDG-quarantine parity backend
  ai/
    client.py         # OpenAI-compatible /chat/completions (stdlib urllib)
    prompts.py        # use-case prompts + output JSON schemas
    guardrails.py     # schema validation, dataset locking, caching, cost meter
  app/                # ── THE PRODUCT: PySide6 desktop application ──
    __init__.py
    __main__.py       # python -m spacesage.app
    main.py           # QApplication bootstrap; probes Qt in a subprocess (no-Qt dialog)
    windows.py        # main window, navigation rail, status bar, plan/settings placeholders
    theme.py          # the one token set: light/dark, spacing, type scale, semantic colors
    models.py         # QAbstractTableModel view-model + hand-painted badge/chip delegates
    state.py          # user settings: theme, target drive, reserve, last export/index
    widgets.py        # metric cards, badges, toasts, empty states, drop zone
    workers.py        # ingest + rank on a QThread -- the UI thread never blocks
    icons.py          # bundled Lucide SVG subset, recoloured per theme
    views/            # import wizard, opportunities + details pane, plan, settings
    assets/           # Lucide SVGs + ATTRIBUTION.md (ISC license)
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
- Materialized derivations: `dir_sizes` (sums from **file rows only** — folder rows include children and double-count), `categories(entry_id, pack, rule_id, category, tier, action, confidence, rationale, native, bytes, is_dir)` (schema v3), `app_footprints(app, bytes, file_count)`.

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

Built-in packs (v0.1): `windows.toml`, `dev.toml`, `browsers.toml`, `media.toml`, `games.toml`, `installers.toml`, `misc.toml` — see slices for content scope. The authoring guide (matcher reference, ordering, tiers, actions) is [`docs/rules.md`](rules.md).

### 6.1 Rules notes (S3 findings)

`spacesage/rules.py` loads the packs (`tomllib`, stdlib), validates them with
actionable errors, matches entries and materialises the classification. The
decisions that became part of the engine:

- **Order is the tool.** Packs carry `[pack] order` (built-ins use 10…90) and
  are sorted by `(order, pack id)` inside their section; **user packs are tried
  before built-ins**, so a user rule can carve an exception out of a broad
  built-in pattern, and a user rule with the same `id` **shadows** the built-in
  one in place. Within a pack, rules are tried in document order.
- **Globs.** `*`/`?` never cross a separator, `**` does, a leading `**/`
  matches zero or more components and a trailing `/**` also matches the folder
  itself (so one rule covers a folder and its subtree). Both separators are
  accepted everywhere. Windows-style paths (drive letter or UNC — i.e. every
  WizTree export) match **case-insensitively**, POSIX paths case-sensitively;
  the same rule decides `name_regex` case handling, so a Windows export behaves
  like the filesystem it came from, whatever machine analysed it.
- **Matchers** are AND-ed (`path` and `ext` lists are OR-ed). `min_size`
  accepts byte counts or `"10 MiB"`-style sizes (binary suffixes); on a folder
  it compares the file-row subtree size. `older_than_days` never matches an
  entry whose timestamp is unknown — "no timestamp" is not "old". A rule
  without any matcher is rejected: no accidental catch-alls.
- **`KEEP` joins the action vocabulary** (`rules.ACTIONS`): the explicit
  *No action* with a reason, used by the least-specific catch-alls
  (`**/Windows/**`, `**/Program Files/**`) so system and app data reads as a
  deliberate decision instead of "unknown". Unmatched entries still fall back
  to `unknown` (T3, `REVIEW`, confidence 0.0) — never destructive.
- **The classifier is per entry, files and folders alike.** Folder rows use the
  same matchers; byte semantics stay S2's: folder sizes are the file-row
  subtree (`stats.iter_dir_sizes`, which now also carries each folder row's
  `name`/`mtime`), and the report's category sizes are file rows, with matched
  folders shown as context.
- **Materialisation.** Schema v3 adds `categories` (one row per entry, cascade
  with the entries, `meta` carries `classify.rules_sha256` — a fingerprint of
  the effective rules). `spacesage classify` is read-only unless
  `--materialize` is passed, and `classify_report()` always recomputes from
  `entries` so the printed numbers can never come from a stale table.
- **Matching stays fast** at export scale: the rule index files ext-based rules
  under every extension they accept and pattern-based rules under a literal
  component each pattern requires (`**/AppData/Local/pip/Cache/**` → `pip`), so
  per-entry work starts from the components the path actually contains;
  candidates are then rejected by a component-subset prefilter before any
  regex runs. A 284k-entry synthetic index (262k files / 22k dirs, deep random
  tree) classifies in ~4 s (≈69k entries/s) and materialises in ~9 s.
- **CLI.** `spacesage classify [--db PATH] [--rules DIR] [--list-rules]
  [--top N] [--json] [--materialize]`; `--list-rules` prints the effective
  order (no index needed), the report covers matched/unknown totals, T1/T2/T3
  roll-ups, per-category sizes and the biggest unknown entries (the rule-author
  worklist).

### 6.2 Candidate generation & scoring (S4 findings)

`spacesage/candidates.py` turns the classifier's verdicts into the ranked
**Opportunities** list (`docs/design.md` §9): per action kind, biggest
estimated win first, every row carrying a suggested solution, its reasons and
the factors behind its rank.  The decisions that became part of the engine:

- **Five kinds.** `delete` (T1/T2 rules with action `DELETE_QUARANTINE` —
  delete always means quarantine), `move` (entries the classifier told to
  relocate), `stale` (big, cold and loosely classified: tier `T2` or unknown,
  untouched for at least `stale_after_days`, default one year; advice-only,
  `REVIEW`), `dupes-weak` (same name **and** size clusters) and `app` (the
  largest application footprints).
- **`move` follows the classifier, not a hard-coded category list**: an entry
  is a relocation candidate when its advice is `MOVE` (media) or `NATIVE`
  (launcher-managed game libraries) and its tier is T1/T2.  That keeps the
  advice in one place — the rule packs — and automatically excludes T3
  (`WinSxS`, OneDrive) and `REVIEW`-only categories (`disk-image`).
- **Files group at directory level.**  A folder the rules matched is a single
  candidate that swallows its matching files (a 1.3 GiB `Videos` folder is one
  row, not three).  Files with no matching folder are grouped by their parent
  directory into one candidate per folder, carrying its largest members (capped
  at `max_members`, default 10), the strictest tier, the *minimum* confidence
  and the *newest* member's age — a group with one recently written file counts
  as recent.  `--min-size` therefore applies to the group total, not to every
  file in it: twenty 80 MiB videos are one 1.6 GiB candidate.
- **Scoring is deterministic and explainable**:
  `score = bytes × tier weight × confidence × recency factor`, with tier
  weights `T1 1.0 / T2 0.6 / T3 0.2`, the classifier's confidence, and a
  recency factor that is `0.5` inside 30 days, rises linearly to `1.0` at 365
  days and is `0.75` when the timestamp is unknown (no timestamp is neither
  fresh nor provably cold).  Every candidate carries the four factors plus the
  age they were computed from, so a rank can be re-derived without the engine.
- **`stale` never guesses.**  It only fires for entries the classifier left at
  `T2` or unknown; a matched `T3` (`KEEP`, `NATIVE`, system data) is a
  deliberate "leave it alone", not a review item, and an entry without a
  timestamp is never "old".
- **`dupes-weak` is weak by construction.**  Name + size agreement is not
  evidence of identical bytes, so the cluster carries `weak = true`, action
  `REVIEW`, confidence 0.3, and `bytes` = the *recoverable* copies
  (`(n-1) × size`, with `member_bytes` showing the combined total).  Hard-linked
  copies are excluded (their payload is already accounted for) and so is
  anything under `dupes_floor` (1 MiB) or under `--min-size` once the
  recoverable total is known.  The hash-verified material is the deep scan
  (S6); these rows exist so the user knows the cluster is there.
- **`app` rows carry the advice of the app's biggest matched entry.**  The S2
  heuristic names the app, the largest matching entry inside it speaks for it
  (a Chrome cache ⇒ `DELETE_QUARANTINE`; the `Program Files` catch-all ⇒
  `KEEP`, i.e. the explicit *No action* with its reason), and an app with no
  matching entry at all is an explicit `REVIEW` with "no rule matched".
- **No double counting.**  Within a kind, a candidate inside another candidate
  is dropped (folder rows aggregate their descendants).  Across kinds, a path
  claimed by a higher-priority kind (delete → move → stale → dupes-weak → app)
  is not listed again, and a duplicate cluster moves to the first copy no other
  kind claimed (or disappears when all its copies are covered).  A *folder*
  candidate that contains claimed entries stays listed — the two rows describe
  different decisions and the plan's selection cascade reconciles them — and
  the report counts every suppressed row.  The exported roots (`C:\`) are never
  candidates, and hard-linked copies are skipped because they free nothing on
  their own.
- **Read-only and streaming.**  One pass over `entries` (the classifier
  iterator) plus one folder pass for the app footprints when that kind is
  requested; only the best `top × 10` candidates per kind are kept in memory
  before collapsing (the buffer leaves room for nested rows to drop out), so
  the lists stay bounded on a 20M-row export.  `--top 0` keeps every candidate,
  and the report says how many were found below the buffer (`found` vs `total`)
  instead of pretending they were suppressed.
- **CLI.**  `spacesage candidates [--db PATH] [--rules DIR] [--kind KIND]…
  [--min-size SIZE] [--top N] [--stale-after-days N] [--dupes-min-copies N]
  [--json]`; `--kind` limits which kinds are *generated*, so the omitted kinds
  cannot claim paths from the listed ones.

### 6.3 Deep scan — verified duplicates (S6 findings)

`spacesage/deepscan.py` is the other half of `dupes-weak`: an **optional** scan
that runs where the files are — the live filesystem, not the export — and
proves which same-size files are byte-identical.  Pure arithmetic, no AI,
strictly read-only; it is the evidence that can promote a weak cluster's bytes
into an executable decision (the plan schema itself does not change here).
The contract:

- **Never follow a link.**  The walk uses `os.scandir` and skips symlinks,
  junctions and every other reparse point: counted (`skipped_links`), never
  descended into, so the scan can neither loop nor read outside the tree the
  user named.  A *root* that is itself a link is refused with a message
  (point it at the real path) instead of being silently followed.
- **Two hash passes, one identity proof.**  Candidates — regular files at or
  above `min_size` (default 1 MiB) — are grouped by size, and a size seen once
  is never read.  The first 64 KiB of every remaining path is hashed and
  grouped; only a group that still holds two members gets its whole content
  read, and the group's `sha256` always covers the entire file, so a shared
  64 KiB prefix is never enough.  Files no larger than the window are covered
  by the first read and are not read twice; a path whose file identity is
  already hashed is not re-read at all (`reused_reads`).  A file that changes
  between the walk and the read is dropped and counted (`changed`), never
  reported as a duplicate of a snapshot that no longer exists.
- **Copies, not paths.**  Members are clustered by file identity
  (`st_dev` + `st_ino`): `copies` counts physical payloads and the recoverable
  figure is `(copies − 1) × size`, never `(paths − 1) × size`.  Verified paths
  that are all *one* payload are reported as a **hardlink set** (`h1`, …) with
  the bytes already saved — deleting them frees nothing, and the report says
  so instead of inviting a pointless cleanup.
- **Keep policy** is `newest-then-shortest-path`: keep the newest copy; ties
  (including "nothing carries a usable timestamp") fall back to the shortest
  path, then lexicographically, so directory order never decides.  Every group
  carries the chosen `keep` and the plain-language `keep_reason`.
- **Same-volume hardlink dedupe.**  Each group carries a `hardlink`
  suggestion: re-create every other physical copy as a hardlink to the kept
  path — the same bytes reclaimed, every path stays valid.  It is `feasible`
  only when all copies sit on one known volume *and* the filesystem reports
  file identities; otherwise the reason says why (hardlinks cannot cross
  volumes) and the extra copies are deleted or moved instead.
- **Report.**  `spacesage.deepscan/v1`: `roots` (absolute; nested roots are
  scanned once, with a note), `thresholds`, `stats` (files, bytes, candidates,
  skips, reads, errors), `summary` (groups, paths, copies, reclaimable bytes,
  hardlink sets) and the `groups` / `hardlink_sets` lists, biggest reclaim
  first with positional ids (`g1`…, `h1`…).  Issues are counted in full and
  sampled in `error_samples`.
- **CLI.**  `spacesage deepscan ROOT… [--min-size SIZE] [--top N] [--json]
  [--progress]`; `--progress` reports the walk and the hash passes on stderr,
  `--top` limits only the text listing (the JSON is always complete).  The
  tests re-stat and re-hash the tree after a scan to pin the read-only claim.

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

### 7.1 Plan notes (S5 findings)

`spacesage/planner.py` composes the ranked candidates into that document. The
decisions that became part of the contract:

- **Every action carries the same keys** (`null` where a type has no use for one):
  `id`, `type`, `kind` (the candidate kind it came from), `path`, `bytes`,
  `category`, `tier`, `confidence`, `rationale`, `why`, `side_effects`,
  `native_alt`, `dest`, `link_after`, `elevation_required`, `command`,
  `covered_bytes`, `weak`. The executor and the GUI can read it without special
  cases; `spacesage.planner.validate_plan` checks it (and the test suite runs the
  rendered document through the validator). `why` is the one-line decision,
  `rationale` the rule's own reason, `side_effects` a plain sentence about what
  the action changes.
- **`plan_id` is a function of the inputs**: `sha256:` over the canonical JSON of
  `{schema, source, actions}` (sorted keys, no whitespace) — the wall-clock
  `created` and the `provenance` block stay outside it, so re-planning the same
  index, rules, targets and reference time reproduces the id exactly (the
  committed golden pins one). Action ids are positional (`a1`, `a2`, … in plan
  order) over a deterministic order, so they are stable for the same inputs;
  the manifest binds to the *plan id*, which changes whenever an action does.
- **`summary`** keeps the three contract keys and adds the rest:
  `delete_bytes`, `move_bytes`, `compress_bytes` (in-place compression, upper
  bound), `review_bytes` (`REVIEW` **and** `NATIVE`: everything not reclaimed by
  SpaceSage), `native_bytes` (the `NATIVE` part of it), `planned_bytes`,
  `covered_bytes`, `dropped` and a `by_type` breakdown. Every byte total is the
  sum of the actions, so a reader can re-add the document.
- **Only the classifier's advice becomes executable** and only at T1/T2:
  `delete` candidates (advice `DELETE_QUARANTINE`) → quarantine actions;
  `MOVE` advice → moves; `NATIVE` advice → `NATIVE` actions carrying the vendor
  command (a launcher-managed library is moved by its launcher, not by us);
  `COMPRESS_NTFS` advice → in-place compression. Everything else — `stale`,
  `dupes-weak` (bytes unverified until the deep scan), `app` footprints,
  `KEEP`/`REVIEW` advice and **any T3 path** — becomes a `REVIEW` item. An app
  row's rationale keeps the advice of the biggest entry inside it, but acting on
  a whole footprint is a human decision, so it is never executable; its vendor
  command rides along in `native_alt`. **T3 is report-only** — no executable
  action may carry it and the validator refuses such a document.
- **Ordering is phase-based**: quarantines (T1 before T2, biggest gain first) →
  moves (biggest first) → compressions → review items (biggest first) then
  native ones. Within a phase the gain used for ranking is the action's *net*
  bytes (what is left after everything already planned inside it), so the list
  stays monotone in the numbers it prints.
- **Move planning resolves what the UI caps.** A folder the rules matched is one
  `MOVE` of the folder; a folder that only *grouped* its matching files is
  re-read from the index and planned as one `MOVE` per file, so the
  `max_members` display cap never truncates a plan, and a file the rules want
  handled by a launcher keeps its `NATIVE` action even inside such a group.
  Destinations mirror the source below `<target>\Moved`
  (`C:\Users\a\Videos` → `D:\Moved\Users\a\Videos`), separator style following
  the target (drive, UNC share or POSIX path).
- **Budgets are strict, targets are predictable.** A target takes moves while
  `bytes <= free_bytes − reserve_bytes` (`--free` states free space for drives
  the machine cannot measure). Targets are tried in the order given, a target on
  the source's own volume is never used (a move there frees nothing), the greedy
  pass skips what does not fit and keeps going for smaller items, and a move
  that fits nowhere is demoted to a `REVIEW` that names the number missing —
  never dropped silently and never over-committed.
- **Link policy** is exactly the design's: directories → `JUNCTION` (no admin);
  files → `SYMLINK` when they leave the volume, with `elevation_required: true`
  because Windows needs the privilege or Developer Mode for those; `HARDLINK`
  only same-volume (and never as the result of a cross-drive move);
  `--no-links` plans `NONE` and says the original path disappears.
- **No double counting.** The ranked list may hold a folder and one of its
  claimed children (a media folder plus a cache folder inside it): the earlier
  action's bytes are subtracted from the later one's, `covered_bytes` records the
  difference, and a candidate left with nothing to act on is dropped and counted
  in `summary.dropped`. The same cascade reconciles app footprints that contain
  planned quarantines.
- **The Markdown generator** (`render_markdown`) is the report/chat rendering:
  header (plan id, source, targets), a summary block, then one section per
  action type with a line per action (path, destination and link, size, tier,
  confidence, why, vendor alternative). `-o FILE` writes `plan.json`.
- **CLI.** `spacesage plan [--db PATH] [--rules DIR] [--to DRIVE]… [--reserve
  20G] [--free SIZE] [--min-size SIZE] [--top N] [--stale-after-days N]
  [--dupes-min-copies N] [--no-links] [--json|-o FILE]`; read-only, prints the
  Markdown summary by default.

## 8. Executor

- `spacesage apply <plan.json> --approve <approved.json> [--execute]`.
- **Windows backend:** moves via `robocopy /MOVE /E /COPYALL` (ACL-preserving) with verify (size + count), `shutil.move` fallback; junction `mklink /J` (no admin) via `cmd /c`; symlink flagged when elevation is required (detect `SeCreateSymbolicLinkPrivilege` / Developer Mode); quarantine = same-volume `_spacesage_quarantine/<plan_id>/…` + manifest; locked files: attempt exclusive open, skip + report.
- **POSIX backend** (dev/tests): `shutil.move`, `os.symlink`, XDG-trash-style quarantine for parity.
- **Journal:** append-only JSONL (`journal.jsonl`): op, before, after, result, verification. `spacesage undo <journal>` reverses in reverse order and verifies.
- **Long paths:** `\\?\`-prefixing on Windows; `MAX_PATH` handling tests.
- **Re-validation** right before every op, and again after (result verification).

### 8.1 Executor notes (S7 findings)

`spacesage/executor/` implements that design. The decisions that are now part of
the contract:

- **The manifest is the gate.** `approved.json` is
  `{schema: "spacesage.approved/v1", plan_id, created, approved: [a1, a2, …],
  rejected: [], note}`; ids are `a<n>`, deduplicated, and the approval is bound to
  the plan's own `plan_id`. A manifest for another plan, an id the plan does not
  have, or a plan that does not validate is refused **before a single op is
  resolved** — there is no "repair" path and no partial run.
- **Dry run is the default and touches nothing.** `spacesage apply` without
  `--execute` resolves every approved action into the exact operation it would
  perform (quarantine destination, move destination, the link that follows) and
  stops: no quarantine directory, no journal (unless `--journal` names one, which
  then records only a `dry-run` header with what was previewed).
- **Quarantine layout.** `<volume root>/_spacesage_quarantine/<plan token>/<volume
  label>/<path components>`. The token is the first 16 hex characters of the plan
  id — the full id lives in the store manifest and in every journal record,
  because `sha256:` is not a legal path component on Windows. Windows puts the
  store on the source's own volume (`C:\_spacesage_quarantine`), so a quarantine
  stays a `rename`; POSIX follows the XDG convention (the home volume goes to
  `$XDG_DATA_HOME/spacesage/quarantine`, any other volume to
  `<mount point>/.spacesage_quarantine-<uid>`, chosen by walking `st_dev`).
  `--quarantine DIR` overrides both. Every store gets a `manifest.json`
  (`spacesage.quarantine/v1`) listing each payload with its size and digest, so
  the store is self-describing when the purge step arrives.
- **Re-validation is a second look, per op, immediately before it runs.**
  *skipped* when the source is gone or locked (or the destination already
  exists); *refused* when the path is no longer absolute, contains a wildcard, is
  a volume root / system directory / profile root / home directory / quarantine
  store, has become a reparse point, or carries the report-only tier; a hard link
  that would cross volumes is refused; `--within ROOT` confines a run to named
  roots. Skips and refusals are reported **and journaled** — "this approved
  action was deliberately left alone" is a decision worth auditing.
- **Verification is a digest, not a hope.** Every payload gets a
  location-independent digest before and after the operation: `tree_sha256` over
  every entry (relative path, kind, size, link target) plus a full
  `content_sha256` for payloads up to 256 MiB — above that the digest says
  "unverified (too large to hash)" instead of pretending. A move that arrives with
  different bytes, or that leaves its source behind, is a *failure*: the report
  says the payload is at the destination and undo can move it back.
- **The journal is written in two phases.** An `op` record with `phase: "start"`
  (the source, the destination and the pre-move digest) is fsynced **before** the
  primitive runs; the `phase: "end"` record (outcome, verification, notes, the
  external command that ran) lands right after. A start record without its end
  record means "this may or may not have happened" — undo settles that against the
  live filesystem instead of guessing, which is what makes an interrupted run
  recoverable.
- **Undo reverses in reverse order, verifying.** The link goes before the move
  that created it, the move before the quarantine it followed, and every payload
  is compared with the digest the journal recorded on the way in. Undo is itself
  journaled (run `undo`, one record per reversal), so a second `spacesage undo`
  reports "nothing to undo" rather than moving things twice. An operation whose
  original path is occupied again is **blocked** and retried on the next run
  (never clobbered); one whose payload was purged is skipped with that reason.
- **Platform parity, stated honestly.** Windows moves run
  `robocopy /MOVE /E /COPYALL` (retried with `/COPY:DAT` when security copying
  needs backup rights, falling back to `shutil.move` when robocopy cannot do it at
  all), directory links are `mklink /J` junctions (no elevation), file links are
  symlinks — and when the process has neither elevation nor Developer Mode the
  **whole action is skipped before the move**, so a path can never end up
  dangling. Hard links are `os.link`; compression is `compact.exe` /c, its undo
  `/u`. POSIX is the reference backend: `shutil.move` (a rename on one volume,
  copy + delete across two, and the report says which), `os.symlink` (a `JUNCTION`
  plan becomes a directory symlink, with a note saying why), `os.link`, an
  advisory `flock` probe for "in use", and NTFS compression refused with a reason
  rather than approximated.
- **The CLI grew two verbs.** `spacesage apply PLAN --approve APPROVED
  [--execute] [--journal FILE] [--quarantine DIR] [--within ROOT]… [--json]` and
  `spacesage undo JOURNAL [--json]`. Both print a readable report (`spacesage
  apply` shows the resolved per-op plan; `undo` shows the reversals) or the JSON
  twin (`spacesage.executor/v1` / `spacesage.undo/v1`), and both exit non-zero
  when an operation failed or was refused — skips are reported but are not
  failures. The journal defaults to `spacesage.journal.jsonl` next to the plan.

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

### 9.2 Desktop app notes (S8 findings)

Decisions the GUI slices after this one have to keep — each of them was a real defect first:

- **A token is one unit everywhere: pixels.** The stylesheet says `font-size: 13px`, so a `QFont` built with `QFont(family, 13)` (points, ~17px at 96 dpi) silently renders 33% larger than the token and the dense table elides half its text. `theme.ui_font`/`mono_font` use `setPixelSize(TYPE_SCALE[...])`.
- **A badge is `text width + BADGE_PADDING`.** Pill text is centred in `rect.adjusted(BADGE_PADDING // 2, …)`, so a pill sized `text + 4` clips the first and last glyph (a `T2` chip painted as `2`). The solution, tier and confidence delegates all size with the constant.
- **The widgets speak paths; the engine speaks keys.** `Opportunity.key` is the case-folded comparison key, but callers hold the path the export carries. `index_of` / `toggle` / `set_checked` normalise through `opportunities.path_key` at that boundary (idempotent), so no caller has to know which form it has.
- **One line, one message.** `row.why` reads `"<action label>: <rationale>"` and the badge already carries the label; the delegate paints the rationale alone and drops it — never squeezes it — below `MIN_WHY` px.
- **Qt is probed in a subprocess.** A missing `libEGL.so.1` aborts the process before a single widget exists, so `--self-check` runs in a child and the parent can still show a real dialog (zenity/kdialog/xmessage) instead of a traceback.
- **Motion is bound to its widget.** The fade's safety timer passes the widget as context object, so a closed window is never repainted by a stale timer — this is the "Internal C++ object already deleted" that pytest-qt's exception capture reports at the *next* test.
- **The list is honest by construction.** `spacesage/opportunities.py` adds the biggest `KEEP`/unmatched entries as `no_action` / `undecided` rows, so the screen never hides what SpaceSage will not touch, and the summary strip counts only rows no other listed row already covers.
- **Screenshots are tests.** `tests/gui/test_screenshots.py` renders the real widgets at 1440×900 offscreen and asserts each render is not a blank frame; the PNGs in `artifacts/gui/` are the same renders the acceptance evidence uses.

### 9.3 Plan & execute findings (S9)

- **The executor validates the *document*, not just the items.** A plan whose action carries a report-only tier, or whose move points outside the declared target drives, is refused whole: `plan_ops` rejects it before a single op resolves, so the screen shows the engine's own sentence and nothing on disk changes. Per-item `refused`/`skipped` outcomes are what a *run* reports; a broken document never starts one. Both paths are surfaced (dialog + per-item status), never swallowed.
- **A nested move needs its parent row checked.** The planner plans a move for a folder's branch, so selecting only `media/holiday.mp4` yields no move at all; checking the `media` row plans it as that branch's entry. The tests select rows the way a user would.
- **A *skipped* operation is not a success.** When the world changed under the plan (the path was gone), the run reports `skipped`; the screen surfaces it as a warning banner with the reason, alongside failures and refusals, because a plan that quietly did less than it promised is the thing a user must not discover later.
- **Column widths are measured, never guessed.** `models.fitted_width` sizes a numeric column from the font the cells use, so "up to 1023.9 PiB" cannot render as "up to …0 MiB"; the why-line beside a badge is elided at a word boundary (`models.elide_words`), so it reads as a short sentence instead of a cut word. Both were real defects in the S8 renders (operator QA note).
- **No test may block on a modal box.** `report_error`/`report_note` wrap `QMessageBox.exec()`; the GUI conftest captures both in an autouse fixture, so a worker that failed unexpectedly fails a test instead of hanging the suite forever. Confirmation and preview dialogs are monkeypatched where a test triggers them.
- **A worker that fails is reported, not raised.** Every screen routes worker failures into one dialog plus a status line; the engine's message is the content, and the run's own state (what already happened) stays on screen.
- **Every long line is elided *or* wrapped on purpose — never clipped.** The plan's meta line and the status bar carry paths, so `widgets.ElidedLabel` elides them (middle for a path, right for a sentence) instead of letting a `QLabel` paint past its edge; the *What happens* column is a sentence, so `plan_models.PlanDetailDelegate` cuts it at whole words (`detail_line`) where the default middle-elision produced "moves to /var/tmp/space…Mode)". The tooltips keep the full text in both cases.
- **The dry-run and confirmation listings are a log panel, not an item list.** A list widget elides a long path (and adds a second scroll axis the user has to discover); `widgets.LogPanel` wraps instead, keeps the text selectable, and separates operations with a blank line. The dialog that lists exactly what will happen must not hide a destination.
- **The confirmation dialog is tested as itself.** Every other execution test replaces `ConfirmDialog`, which is exactly how a confirmation that lists nothing (or is styled like a friendly question) ships green; one test now builds the real dialog and asserts its itemized lines, its title and its danger-styled accept button — and the screenshot pass renders it (`confirm.png`).
- **A checkable cell needs a keyboard path.** Space toggles the row under the cursor in all three lists (`widgets.space_toggles`), so checking, approving and selecting-for-revert are not mouse-only; an advice row refuses and the status line says why.
- **The fixture carries an advice row on purpose.** `archive/setup.msi` (an installer older than a year outside Downloads → T2 `REVIEW`) makes "advice is listed, never executed" a tested path instead of a `pytest.skip`: the plan shows it as *Advice only*, the run's report never contains it, and the screenshot shows a plan with 4 executable actions and 1 advice.
- **A listing dialog sizes itself around its listing.** `dialogs.LogDialog` (the base of both `ConfirmDialog` and `PreviewDialog`) wraps the itemized lines, then widens to the longest one and grows until its scrollbar reports nothing hidden — capped by the window it opens in, never by the screen. A preview whose last operation sits below the fold is a preview the user has to hunt through, and `QPlainTextEdit`'s document counts *blocks*, not wrapped rows, so the fit is measured from the scrollbar (in lines) rather than estimated.
- **A toast must not cover the button it reports on.** `Toast.pop_up` centres over the window but takes an `above=` anchor (the plan toolbar, the undo action row) so it floats just above the controls the user is about to press; a second toast replaces the first instead of stacking on it in the same spot, where the two boxes hide each other's text.

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
spacesage deepscan <root>… [--min-size 1M]    # hash-verify exact duplicates (live, read-only)
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
