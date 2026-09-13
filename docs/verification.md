# Verification

What this project proves before it calls a version done, where the proof lives,
and how to run it yourself.

## The acceptance pass (`tests/e2e`)

The unit suites prove each module and each screen on its own; the acceptance
pass proves the *product*: one planted "full disk" is driven through every
stage the engine has and through the app itself, out of process, the way a
user and CI run it (`python -m spacesage ...`, `python -m spacesage.app ...`).
Nothing is stubbed between the stages: the CSV the scenario writes is the CSV
the pipeline ingests, and the plan the pipeline prints is the plan the executor
is handed.

```bash
source scripts/gui-env.sh          # Qt's libraries, for the smoke pass
.venv/bin/python -m pytest tests/e2e -q
```

About a minute and a gigabyte of scratch space. CI runs it as the `e2e` job
(`.github/workflows/ci.yml`) on one Linux leg and uploads `artifacts/e2e` as
the run's evidence; the cross-platform test matrix skips the pass
(`--ignore=tests/e2e`) because the scenario is POSIX-shaped on purpose.

A release is also verified the way a stranger gets the code: `git clone` into a
different directory, `uv sync --frozen --extra build`, the whole suite. That is
not ceremony -- the check is what caught the committed plan fixture carrying a
machine-specific `rules_fingerprint` (it hashed each rule's pack path, so the
golden could only be reproduced by the machine that generated it); the
fingerprint now covers what a rule does and not where its pack file lives
(`spacesage/rules.py`).

### The disk it plants

`tests/e2e/scenario.py` builds the tree from fixed sizes, fixed ages and a
fixed reference moment, so every number a stage prints can be asserted exactly:
OS scratch space, browser profiles, developer trees, personal media, a game
library, downloaded installers, one installer old enough to go, a disk image
and a partial download that must stay advice, one file no rule matches, one
file below every size floor, and two byte-identical `blob.bin` copies for the
deep scan. Alongside the tree it writes the WizTree export (sizes, mtimes and
folder totals read back off the disk, so the export can never describe
something the disk does not have), an index for the pipeline, a second empty
index the app builds itself, and a target drive on a *different* volume --
on the same volume a move frees nothing, and the planner refuses it.

### What it proves

| Test | A failure would mean |
|---|---|
| `test_the_scenario_plants_a_full_disk` | the planted tree does not match the sizes, ages and duplicate pair the rest of the pass assumes |
| `test_the_scenario_planted_at_the_reference_path` | the disk is not where the export says, or the target drive sits on the source's own volume |
| `test_the_export_describes_the_planted_tree` | the export's rows lost a file, a folder total or a hardlink marker on the way out |
| `test_ingest_indexes_exactly_what_was_planted` | the parser dropped or duplicated rows, or miscounted bytes |
| `test_classify_matches_the_disk_and_names_the_categories` | the rule engine matched the wrong entries, or the category totals do not add up to the index |
| `test_candidates_are_real_and_ranked` | the ranked list names something that is not on the disk, ranks the sizes wrong, or lists an entry below the floor |
| `test_the_plan_is_complete_consistent_and_safe` | the plan is missing an executable action, its action/byte totals disagree with its own actions, or an executable action is nested inside another |
| `test_two_plan_runs_over_the_same_index_have_the_same_id` | the plan is not deterministic (the id is the approval manifest's key) |
| `test_dry_run_execute_and_undo_restore_the_disk` | a dry run touched the disk, the executed set differs from the approved set, or undo did not put every entry back byte for byte |
| `test_the_executor_gates_hold` | a foreign manifest executed a plan, an item outside the sandbox was touched, or a rejected plan mutated the disk |
| `test_deepscan_verifies_the_duplicate_pair` | the hash-verified groups do not match the planted pair, or a copy that must be kept is offered for removal |
| `test_every_stage_agrees_on_the_numbers` | ingest, classify, candidates and the plan disagree about entries, bytes or the reclaimable total -- and the Markdown report disagrees with the JSON |
| `test_the_app_runs_the_whole_loop_on_the_full_disk` | the app cannot walk its five screens on a real export: the analysis, the ranked list, the plan it builds from checked rows, the dry run, the execute and undo back to the planted tree -- or a screen renders clipped, or the app reports an error |
| `test_the_app_boots_and_renders_its_first_screen` | the real entry point cannot start, render and exit cleanly (`--capture`) |

The smoke pass also watches the run: Qt's message handler, `sys.excepthook` and
`threading.excepthook` are captured for the whole loop, every dialog the app
would have shown is intercepted, and the pass fails if anything beyond the
platform's known chatter arrives.

### The evidence it leaves

Every run writes `artifacts/e2e/` (uploaded by CI, attached to the slice's
card):

| File | What it is |
|---|---|
| `smoke-1-import.png` .. `smoke-5-settings.png` | the five screens as the app renders them on the planted full disk, at the reference window (1440x900) |
| `smoke-boot.png` | the app's own first frame, from the real entry point |
| `smoke-boot-stderr.txt` | what the real entry point said while it started (should be empty) |
| `smoke-analysis.txt` | the ranked list the app produced, row by row |
| `smoke-run.txt` | the plan, the dry run, the execute, the undo and the restored tree |
| `smoke-footer.txt` | the action bar's figures and their measured widths (a bar that silently clipped its own figure would be caught here) |
| `pipeline-run.txt` | the same for the CLI pipeline: stage outputs, plan summary, applied ops |
| `demo-plan.json` | the plan the pipeline built for the full disk, with its report |

### Known limits

- **POSIX only.** The scenario needs real symlinks and a second temporary
  volume; on a host that cannot serve it the suite skips with the reason
  (`scenario.unavailable_reason()`) instead of failing. The Windows-only
  executor paths have their own suite (`tests/test_executor_win.py`).
- **The renders carry the run's clock.** Timestamps in the screens mean
  `artifacts/gui/*.png` go stale on every run (the same wart the screenshot
  suite has always had).
- **CPython's singletons are parked for the run (Python 3.11).** The locked
  PySide6 corrupts the reference counts of `None`/`True`/`False` on Python->C++
  calls, in both directions: one `QApplication.processEvents()` call drops
  `None` by one on 3.11.15 (`4611686018427387903` -> `4611686018427367903` over
  20000 calls, with the live-object and `gc.get_referrers(None)` counts
  unmoved), and an app-shaped window lifecycle *adds* about 2000 per window.
  On 3.11 the singletons are ordinary refcounted objects, so a long enough run
  drained them and the interpreter aborted while finalizing -- *after* an
  all-green summary (`Fatal Python error: bool_dealloc ...`, exit 134), which
  is how this suite used to lie about `109 passed`; 3.12+ never shows it,
  because the singletons are immortal there (PEP 683). The session now parks
  them out of reach (`tests/qt_shutdown_guard.py`, applied by
  `tests/conftest.py`) -- what 3.12 does natively -- and
  `tests/gui/test_shutdown_guard.py` holds both halves: a child that pumps
  20000 turns exits 0, and without the guard it dies at exit. The drain needs
  accumulation across files (`tests/gui` alone reproduced it; a single file
  exited 0), hence the session-wide guard. The app's own runs have not been
  observed to abort (the leak direction dominates there), but their counts are
  wrong in the same way; card t_1b70d03f carries the measurements.
- **At the smallest window the bars elide; they no longer clip.** The shell
  allows a 980x620 window (`MainWindow.setMinimumSize`) and several rows need
  more width than that, so what cannot fit now gives way *with a sign*: the Plan
  toolbar's figure paints `0 of 0 executable actions approved · 0 B to recla…`
  (137px given, 599px needed on the full-disk plan this page measures), the
  Opportunities hint `Selecting a folder covers its contents: every byte is
  counted on…` (392px of the 398px it needs), the Undo footer's figure, and the
  summary strip's card titles (`ESTIMAT…`). The two squeezed bars also elide
  captions they cannot pay for: `Dry-run preview`,
  `Open journal file…`, `Revert selected` and `Revert all pending` -- the last
  one is what the destructive action of the Undo screen says at that size, and
  a plain `QPushButton` cut it at *both* ends (`vert all pendin`). Every elided
  bar hands its full text over in a tooltip, and `tests/gui/test_min_size.py`
  walks every screen at 980x620 *and* at 1440x900 and fails if any visible label
  or button caption is cut without one; it failed on the code before the fix,
  for the label and for the caption. What no label or caption can fix is the
  row itself: at 980x620 the Plan toolbar asks for 1192-1205px (its five
  buttons at their minimums plus the figure -- 1192px in the state this page
  measures, 1205px on the S12 plan) and is given 736px, the Undo footer's four
  buttons plus its figure need more than the row has, the summary strip's five
  cards are squeezed, the filter bar's search box is given 46px and the Plan
  table needs its horizontal scrollbar. Those bars
  have to reflow (or the shell has to raise its minimum) before they *fit*;
  they are honest about it until then.

## Where every slice's feature lives

| Slice | Feature | Code | Tests | Docs |
|---|---|---|---|---|
| S0 | Scaffold | `pyproject.toml`, `spacesage/__init__.py` | `tests/test_smoke.py` | `README.md` |
| S1 | Ingest (WizTree CSV → SQLite) | `spacesage/ingest.py`, `spacesage/db.py` | `tests/test_ingest.py`, `tests/fixtures/gen.py` | `docs/design.md` §3, §5 |
| S2 | Stats and aggregates | `spacesage/stats.py` | `tests/test_stats.py` | `docs/design.md` §3 |
| S3 | Rule packs and classifier | `spacesage/rules.py`, `spacesage/rules/*.toml` | `tests/test_rules.py`, `tests/fixtures/gen_rule_packs.py` | `docs/rules.md` |
| S4 | Opportunities and scoring | `spacesage/candidates.py`, `spacesage/opportunities.py` | `tests/test_candidates.py`, `tests/test_opportunities.py` | `docs/design.md` §6.2 |
| S5 | Plan generator | `spacesage/planner.py`, `spacesage/planning.py` | `tests/test_planner.py`, `tests/test_planning.py` | `docs/design.md` §7 |
| S6 | Deep scan (duplicate groups) | `spacesage/deepscan.py` | `tests/test_deepscan.py` | `docs/design.md` §3 |
| S7 | Executor and undo | `spacesage/executor/` | `tests/test_executor*.py` | `docs/design.md` §8, `docs/safety.md` |
| S8 | Shell, Import, ranked list | `spacesage/app/` (`windows`, `state`, `theme`, `models`, `widgets`, `workers`, `views/import_view`, `views/opportunities_view`, `views/details_pane`) | `tests/gui/test_shell.py`, `tests/gui/test_import_view.py`, `tests/gui/test_opportunities_screen.py` | `docs/design.md` §9, §9.1 |
| S9 | Plan, execute and undo screens | `spacesage/app/views/plan_view.py`, `spacesage/app/views/undo_view.py`, `spacesage/app/plan_models.py`, `spacesage/app/undo_models.py` | `tests/gui/test_plan_view.py`, `tests/gui/test_undo_view.py` | `docs/app-guide.md` |
| S10 | AI engine | `spacesage/ai/` | `tests/test_ai_*.py` | `docs/ai.md` |
| S10b | AI in the list | `spacesage/app/ai_models.py`, `spacesage/app/views/opportunities_view.py`, `spacesage/app/views/settings_view.py` | `tests/gui/test_ai_screen.py` | `docs/ai.md` |
| S11 | Packaging and the guide | `packaging/spacesage.spec`, `packaging/win_version.py`, `scripts/make_app_icons.py`, `spacesage/app/assets/` | `tests/test_packaging.py` | `docs/design.md` §14, `docs/app-guide.md` |
| S12 | Acceptance pass and v0.1.0 | `tests/e2e/` | `tests/e2e/test_pipeline_e2e.py`, `tests/e2e/test_gui_smoke.py` | this page, `CHANGELOG.md` |
| S12b | The smallest window: bars elide, never clip | `spacesage/app/widgets.py` (`ElidedLabel`, `ElidedButton`, `caption_room`), `spacesage/app/views/plan_view.py`, `views/opportunities_view.py`, `views/undo_view.py` | `tests/gui/test_min_size.py` | this page, `docs/design.md` §9.1 |

The module list is the same one `README.md` and `docs/design.md` carry; this
table only says *which slice* put it there and which tests hold it up.

## The release

`v0.1.0` is the tag of the S12 commit (`git tag -a v0.1.0`). The release
workflow publishes the Windows executable and the Linux bundle for it, and
refuses a tag whose version does not match what the package reports
(`spacesage --version`).
