# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is 0, a minor bump may change behaviour: the plan schema
and the approval manifest are versioned separately and refuse versions they do
not understand.

## [Unreleased]

## [0.1.10] - 2026-10-01

### Changed
- **The Import screen only chooses what to read.** It now shows two ways in side
  by side — a *New export* card (drop a CSV or browse) and a *Last analysis*
  card carrying the index's row count and age (*Re-analyze* ranks it again) —
  and the one analysis option, relabelled *Ignore files smaller than*. The move
  settings left the screen: *Target drive* and *Reserve on target* are decisions
  about the plan, asked before you had seen a single row. They are now the
  **Where should moves go?** card on the **Plan** screen (*Send moves to* and
  *Leave free on target*, with a live `free − reserve` budget line), and
  *Build plan* in the ranked list carries the checked rows there so the target
  is chosen before the plan is drafted.

## [0.1.9] - 2026-10-01

### Added
- **Charm Hyper is a preset.** `charm` fills in the OpenAI-compatible gateway
  at `https://hyper.charm.land/v1`, the key variable `HYPER_API_KEY`
  (`sk-hyper-…`) and the default model `deepseek-v4.1-flash`, so a charm.land
  subscription is a one-click add in Settings instead of a hand-written
  `custom` provider. The preset carries that model's published prices
  ($0.30 / $1.20 per 1M tokens) so the cost meter works out of the box; the
  tooltip says to update them when switching to a differently priced model in
  the catalog. No special headers are sent (the OpenCode routing headers stay
  scoped to `opencode.ai`).

### Changed
- **Shared helpers have one home each.** Four copies of `_iso`, five of
  `_plural`, two each of the TOML escapers and `_ext_of`, two each of the CLI
  `--kind` / `--min-size` parsers, and two each of the executor's required /
  optional record-field readers were private copies in the modules that needed
  them, so a fix had to be repeated or it drifted. They now live in
  `spacesage._util` (`iso_utc`, `plural`, `toml_string`, `toml_number`,
  `ext_of`), `spacesage._cliargs` (`kind_list`, `size_arg`) and
  `spacesage.executor._fields` (`required_str`, `optional_str`), shared by the
  engine, the AI layer and the executor. No behaviour changed.
- **The CLI parser builder is split by subcommand.** `spacesage.cli`'s
  `build_parser` was a single 458-line function; it is now a short composition
  over one `_add_<command>_parser` helper per subcommand (`deepscan` through
  `undo`), so a command's arguments are read in one place.
- **Three private functions called `_path_components` are renamed to say what
  they do** (`_component_count` in `deepscan`, `_glob_literals` in `rules`,
  `_split_path` in `stats`), so grepping the name no longer returns three
  different jobs.
- **`mypy --strict` now passes on Windows.** The per-OS directory helpers read
  `sys.platform` through a widened `str` (`spacesage._util.PLATFORM`, or a
  local `_PLATFORM` in the stdlib-only crash reporter), so `warn_unreachable`
  no longer folds the `darwin` branch away on Windows; the guarded `fcntl`
  import is treated as `Any` because typeshed strips its `LOCK_*` constants
  off Windows. This also surfaced and fixed a latent variable-type bug in
  `crash.crash_log_dir`. The gate runs on Linux in CI either way.

### Fixed
- **The candidates CLI test no longer drifts with the calendar.** The suite was
  red on 2026-10-01, not because anything broke but because
  `test_cli_json_matches_the_engine` ran the command line on the wall clock
  while its expected list is pinned to the fixture's reference time
  (2026-09-12): candidate ages grew, the recency factor inside every score
  moved, and the ranked order stopped matching — the same class the planner
  test was already fixed for. Both the ranked-list and the JSON test now run
  the CLI on the fixture's own clock (`--now`), through a `candidates_cli_args`
  helper mirroring the planner's.

## [0.1.8] - 2026-09-23

### Fixed
- The completion ceilings leave room for models that reason before answering.
  Every use case sent `max_tokens` sized for the answer alone (1600 for a
  suggestion batch), so a reasoning model spent the whole budget on its
  reasoning pass and answered nothing: OpenCode Zen's `deepseek-v4.1-flash`
  returned empty content with `finish_reason=length` on most batches, and the
  app reported "answered without content - check the model name", which was
  never the problem.  A ceiling is not an allocation -- it only binds when a
  model would otherwise be cut off mid-answer -- so the use cases now send
  4096 (3072 for the short summary), and an answer actually cut short is coded
  `truncated` with the real explanation instead of `bad_response`.

## [0.1.7] - 2026-09-23

### Fixed
- An auth refusal quotes the provider's own explanation.  OpenCode Zen
  refuses its free models to any client but its own with
  `403 FreeTierError: ... can only be used from within OpenCode`, while
  *Test connection* passes (`/models` needs no key at all) -- and the app told
  the user to "set the key environment variable" although the key was set and
  sent.  A 401/403 error body's own message now rides in the error, and the
  hint names where the key came from instead of asking for one.

## [0.1.6] - 2026-09-23

### Fixed
- The packaged app ships the built-in rule packs.  The PyInstaller spec
  bundled the icon assets but not `spacesage/rules`, so a released
  `spacesage.exe` imported and indexed a WizTree export and then failed every
  analysis with "built-in rule packs are missing at ...\spacesage\rules"
  (every exe release had this; the smoke steps only ever booted the window).
  The spec now bundles the packs, `--self-check` reports `rules N packs M
  rules` beside the font line, and the spec test, both CI package jobs and
  both release smoke steps fail a bundle that forgot its data.

## [0.1.5] - 2026-09-23

### Fixed
- WizTree 4.31 exports import again.  The new version writes a
  `Generated by WizTree ...` note before the header (which the reader took for
  the header and refused) and stamps `Modified` with dots (`2026.09.22
  20:17:51`, not `2026/09/22 ...`), which every row read as an invalid
  timestamp.  The banner is skipped, both separator styles parse as the same
  zone-less local time, and the drive-summary columns WizTree 4.31 appends
  after `Folders` (only the root row fills them) no longer count every data row
  as short.
- The Settings key field refuses a pasted API key.  It names an environment
  variable, so a key typed there was written into `ai.toml` (which promises
  never to hold keys) and could never resolve - actual calls fail with `auth`
  while *Test connection* still passes, because `/models` needs no key.  The
  value is now checked on save and by the validated accessor every call uses,
  and refused with the sentence that says what the field is for.  The provider
  list also marks which entry is the default, the one every call uses.
- The AI configuration tests no longer read the developer's real `ai.toml`
  (`AIConfig.load(env={})` fell back to the platform config directory, so a
  configured machine failed the "off until configured" test), and the smoke
  test that pins the released version catches up to 0.1.4 (the release commit
  bumped `__version__` but left the test at 0.1.3, so the suite was red).

## [0.1.4] - 2026-09-23

### Fixed
- The Settings screen no longer crashes on a provider that is not filled in
  yet.  A `custom` provider is a blank template until someone sets its
  `base_url`, but the AI card read providers through the validated accessor
  (the one a *call* goes through), so listing or reloading such a provider
  raised `invalid_config` and took the whole window down — the crash a user
  hits right after adding the blank preset.  The card now reads providers
  unvalidated; the validated door every call uses is unchanged.

## [0.1.3] - 2026-09-22

### Fixed
- The exe smoke steps wait for the windowed build to exit before reading
  its numbers (PowerShell returns immediately for GUI-subsystem
  processes, so the render check used to race the app and read the PNG
  before it existed).
- An unhandled exception during startup now writes a full traceback to
  `<data dir>/logs/crash-<stamp>.log` and shows a message naming that file
  (a native Windows message box where Qt cannot show one), instead of the
  frozen bootloader's one-line "Failed to execute script" box with nothing
  to forward.
- The Windows release smoke renders on the native Windows platform: the
  offscreen plugin does not rasterize text there, so the shipped render
  showed boxes instead of the window's words.  The smoke now fails when
  the render is too small to contain text.

### Added
- `--self-check` reports the resolved UI font and the system's font count;
  the release smoke prints it into the run's annotations, so a build that
  cannot see fonts is visible without opening the PNG.

## [0.1.1] - 2026-09-22

### Added

- **OpenCode Zen preset**: the provider list offers OpenCode Zen
  (`https://opencode.ai/zen/v1`, key from `OPENCODE_API_KEY`, default model
  `deepseek-v4-flash`), and the client sends the routing headers it requires
  (`x-opencode-session`, `x-opencode-client`) on every request aimed at the
  gateway -- chat, streaming, the connection test and model refresh.

### Fixed

- **Hard-link detection works on Windows**: the scan reads the file identity
  from a following stat, because the no-follow stat there reports no file
  index -- hard-linked twins are detected as one physical copy, not as
  reclaimable duplicates, and their link count now reads from the same stat
  (the no-follow one says 1 there).
- **The executor honours the path style it is given, on every host**: POSIX-shaped
  paths are joined, rooted and judged with POSIX rules even when the host is
  Windows (which silently mangled "/mnt/q" before), and the Windows branch now
  refuses the home directory itself like the POSIX one does.

## [0.1.0] - 2026-09-13

The first release: the engine, the desktop app, the optional AI layer, the
packaged build and the end-to-end acceptance pass that proves them together.

### Added

- **Desktop app (PySide6)**: the shell (rail, status bar, theme token set with
  light/dark/system), the Import screen (WizTree CSV, target drive, reserve, size
  floor, worker-thread analysis), the ranked Opportunities list with the details
  pane, the Plan screen with approval, dry-run preview, itemized execute
  confirmation and per-item results, the Undo screen over the engine's journals,
  and Settings (appearance, AI, index).
- **Optional AI layer** over any OpenAI-compatible endpoint: per-row suggest /
  classify / explain, batch fill for the undecided rows with a cost pre-flight and
  a local cache, plan review with severity-tagged annotations, "apply as rule"
  with a dry-run TOML preview, provider management with a connection test, and
  the privacy switches (`redact_paths`, local-only). The AI advises; it never
  executes, approves or writes a plan.
- **Analysis engine** (stdlib-only, `pip install spacesage`): WizTree CSV ingest
  (BOM/UTF-16, quoted fields, extra columns, hardlink markers), stats, the rule
  engine and pack format, opportunity scoring, plan building with a deterministic
  `plan_id`, `deepscan` for verified duplicate groups, the journaled executor
  (quarantine, move-verify-delete, link, compress, native-tool advice) and undo —
  all reachable through the internal CLI.
- **The CLI can pin its reference time, so a run is reproducible on any day.**
  `classify`, `candidates` and `plan` take `--now WHEN` (ISO 8601 like
  `2026-09-12T12:00:00Z`, or epoch seconds) -- the reference point every age and
  recency calculation in the invocation is computed from. The engine always
  accepted one (the plan's `provenance.as_of` records it, and `plan_id` is
  defined against it), but the CLI could only use the wall clock: the day counts
  inside candidate rationales changed from one day to the next, so a run could
  not be reproduced verbatim from the command line. The same index, rules,
  targets and `--now` now reproduce the same categories, ranks, plan and
  `plan_id`.
- **Safety model**: risk tiers with report-only T3, read-only analysis,
  manifest-bound execution, per-item re-validation, two-phase journals with
  payload digests, quarantine instead of deletion, undo that verifies before it
  moves back.
- **Packaging**: windowed single-file PyInstaller build
  (`packaging/spacesage.spec`, `[build]` extra), app icon drawn once in
  `spacesage/app/assets/app-icon.svg` and rendered into every derivative by
  `scripts/make_app_icons.py`, Windows version resource generated from the
  package version, frozen-safe Qt self-check, `--capture` screenshot smoke flag,
  a CI job that builds and runs the bundle, and a tag-triggered release workflow
  that publishes the Windows executable and the Linux bundle.
- **Docs**: README (app first), `docs/quickstart.md`, `docs/app-guide.md`,
  `docs/safety.md`, `docs/faq.md`, plus the engine references and the design
  document.
- **Acceptance pass** (`tests/e2e`, run in CI as the `e2e` job): a planted
  "full disk" scenario drives every engine stage out of process
  (ingest → classify → candidates → plan → report → dry run → execute → undo,
  with the tree verified restored byte for byte) and then the app itself
  through all five screens, with the renders and the run logs kept as evidence
  (`artifacts/e2e/`). `docs/verification.md` maps every slice to the code,
  tests and docs that carry it.

### Fixed

- **The Qt suites exit 0 on Python 3.11, so CI's 3.11 leg stops lying.** The
  locked PySide6 corrupts the reference counts of CPython's singletons on
  Python->C++ calls; on 3.11 a long enough run drained `None`/`True`/`False` and
  the interpreter aborted while finalizing *after* an all-green summary (exit
  134). The test session now parks them out of reach
  (`tests/qt_shutdown_guard.py`, applied by `tests/conftest.py`), which is what
  CPython 3.12 does natively, and `tests/gui/test_shutdown_guard.py` holds both
  halves: twenty thousand event loop turns exit 0, and without the guard the
  same child dies at exit. That fix is test-side only.
- **The app parks the singletons too, so a long Python 3.11 session cannot
  abort it.** The corruption the suites hit runs in the product as well, and
  the product's sessions do drain: re-adopting a listing drops about 92
  references to `None`, a status-bar update 3, a theme switch ~12, and a
  `resize` or an event-loop turn 1 each, while only *building* a window adds
  (~1500 -- and the app builds one per run). A simulated session of exactly
  those calls (status update and window drag per round, a theme switch every
  25th, a re-analysis every 100th) drained 11.9 references to `None` a round
  and the interpreter aborted at finalization (exit 134); a window drag drains
  2 per frame, and 20000 event-loop turns abort it mid-run. The product boot now
  parks them (`spacesage/app/qt_shutdown_guard.py`, the same function as the
  harness's guard) from `spacesage/app/main.py`, before any QApplication is
  built, and `tests/gui/test_app_shutdown_guard.py` holds it out of process: a
  child that boots through `spacesage.app:main` and pumps 20000 event-loop
  turns exits 0. `docs/verification.md` carries the measurements.
- **The action bars no longer clip at the shell's smallest window.** At 980x620
  a row can need more width than it is given, and a plain `QLabel` or
  `QPushButton` then paints past its own edge: the Plan toolbar's figure read
  `0 of 0 executable actions approved · 0 B to recla…` with nothing to say a
  word was missing, and `Revert all pending` on the Undo footer was cut at
  *both* ends (`vert all pendin`) on the destructive action of that screen. The
  bars' figures and the captions they squeeze now elide instead — visibly, with
  the whole text one hover away: `ElidedLabel` gained `claim_width` and the
  "a line that fits exactly is not elided" fix, `ElidedButton` is new, and the
  Plan toolbar's five buttons and the Undo footer's four use it.
  `tests/gui/test_min_size.py` walks every screen at 980x620 and at 1440x900
  and fails if any visible label or button caption is cut without a tooltip; it
  failed on the code before the change. The bars themselves still needed more
  width than 980x620 gave (the Plan toolbar's minimum is 1192px), so they elided
  where they could not fit — the entry below reflows them instead; see
  `docs/verification.md`, Known limits.
- **The six action bars reflow at the shell's smallest window instead of eliding
  everything.** The bars the entry above made honest still *needed* more width
  than a 980x620 window gives — the Plan toolbar 1192px, the Undo footer 949px,
  the Opportunities strip's six cards 837px, the filter bar's search down to
  46px — so at that size a user read ellipses and squeezed controls everywhere.
  Each of the six rows is a `widgets.FlowLayout` now: a row that runs out of
  width moves its last items onto a second line, and every figure, caption and
  card title is whole at 980x620 *and* at 1440x900 (where all six are back on one
  line, packed from the left — the two bars used to right-align their buttons
  with a stretch, so at the reference size the toolbar's buttons sit 4px further
  left than they used to and the Undo footer's hint and buttons 77px further
  left).
  `FlowLayout` gained the one thing a plain wrap would lose: `addWidget(widget,
  stretch)` gives a line's leftover width to the items that ask for it, so the
  filter bar's search keeps its full 490px at the reference size while the bar
  still wraps at the minimum (235px there, where the old bar gave it 46px). The
  elision the entry above added is unchanged — it stays what text no line can
  hold falls back to. `tests/gui/test_min_size.py` walks the six rows at both
  sizes and fails if one of them elides text it could have wrapped; five of its
  tests failed on the code before the change. The renders are regenerated and
  `docs/verification.md` says which rows take a second line.
- **The planner's CLI-vs-engine agreement test no longer drifts with the
  calendar.** `test_cli_json_matches_the_engine` ran the command line on the
  wall clock and the engine on the fixture's pinned reference time
  (2026-09-12 12:00 UTC). The two agree only while both clocks truncate to the
  same age in whole days -- and those ages sit inside the action rationales that
  `plan_id` hashes, so the test was green only inside the pin's first day and
  went red once the calendar moved on (first reproduced 2026-09-22, ten days
  after the pin). Both sides now run on the fixture's reference time through the
  new `--now`, which the planner CLI tests exercise on every call.

### Notes

- The packaged artifacts are not committed: CI builds them, the release workflow
  publishes them.
- `v0.1.0` is an annotated tag on the release commit; the release workflow refuses a
  tag whose version does not match `spacesage --version`.
