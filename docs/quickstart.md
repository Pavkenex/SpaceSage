# SpaceSage — quickstart

From a WizTree export to a finished cleanup in five minutes: install the app,
import the export, review the ranked list, approve a plan, run it — and know
exactly how to put it back.

The one rule that never changes: **analysis is read-only, and nothing on your
disk is touched until you approve a plan and confirm the run.** Details in
[safety.md](safety.md).

---

## 1. Install the app

### Windows (the packaged executable)

Download `spacesage.exe` from this project's releases page and run it. It is a
**single portable file** — no installer, no console window, nothing to
configure. Python is not required.

The first start scans your filesystem only when you ask it to (Import); the app
itself opens on the Import screen with an empty index.

### Linux (the packaged bundle)

Download `spacesage-linux-x86_64.tar.gz`, unpack it, and run `./spacesage`.
Qt's runtime libraries are the usual missing pieces on a bare machine:

```sh
sudo apt-get install -y libegl1 libgl1 libglvnd0 libxkbcommon0
```

The app assumes a desktop session. On a headless machine it can still run the
analysis (see *Without a display*, below).

### From source (development)

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/):

```sh
git clone <this repository> && cd spacesage
uv venv .venv
uv pip install -e '.[dev]'          # or simply `uv sync`
uv run python -m spacesage.app      # the window
```

`spacesage-app` (the installed GUI script) does the same thing. The engine alone
installs without Qt: `pip install spacesage` — see *Engine library & CLI*.

---

## 2. Export your tree from WizTree

In [WizTree](https://www.diskanalyzer.com/): scan the drive, then **Export to
CSV** (a WizTree feature, not this app's). Keep the default seven columns. The
export is the input for everything below, and it is the *only* thing SpaceSage
reads: it never scans your disk itself in v0.1.

Notes that save time later:

- Scan **as administrator** for a complete picture (some system folders are
  invisible without it) — the export is still just data, so there is no risk in
  looking.
- The export of a large drive is big; 1–20 million rows are expected and the
  import is built for that.
- An export from **another** machine works for analysis (the drive letters and
  paths are read as they were exported). Only moves need a destination that
  exists here.

---

## 3. Import it

![The Import screen](../artifacts/gui/import.png)

1. Drop the CSV on the zone, or **Browse for export**.
2. **Target drive** — where you want moves to go (e.g. `D:`). Type the drive (or
   an absolute folder) exactly as it appears on *this* machine. If the app
   cannot measure its free space, it says so and asks you for the number when
   the plan is built.
3. **Reserve on target** — leave this at the default (20 GiB) unless you have a
   reason. It is the free space the planner must never touch.
4. **Smallest entry** — 100 MiB by default. Lower it if you are hunting for
   smaller wins; raise it if the list feels long.
5. **Analyze**. The status bar counts rows per second; the window stays usable.

When it finishes you land on the Opportunities screen. If you already have an
index for this export, **Use the existing index** skips straight there.

---

## 4. Review the list

![The Opportunities screen](../artifacts/gui/opportunities.png)

Biggest win first, files and folders together. Read it like a colleague's
recommendation list rather than a delete button:

- **Has action** rows are ones the rules would act on. **T1** (scratch, caches)
  is the safe end; **T2** (regenerable but meaningful: package stores, project
  artifacts, installers, media) is where you should look twice.
- **No action** rows show the engine's reason for leaving something alone —
  read a few, they are the pack's opinions on record.
- **Undecided** rows are ones no rule recognised: nothing will ever propose
  touching them, and that is where an optional AI provider helps most.
- **"up to N MiB"** in *Est. gain* means an unverified upper bound (weak
  duplicates: same name and size, not proven identical). Verified duplicate
  groups come from the CLI's `deepscan`, never from a guess.
- Tick the rows you agree with. Ticking a folder takes its contents with it; the
  action bar counts the gain once, not twice. **Select visible** after filtering
  is the usual fast path.

The details pane explains whatever you click: the rule, the reasoning, the side
effects, the alternatives, and where a move would land.

---

## 5. Build the plan

![The plan screen](../artifacts/gui/plan.png)

**Build plan** turns the checked rows into one plan document:

- the header shows the plan's `sha256` and its workspace folder — that is the
  document you are about to approve;
- actions carry their tier and, for advice rows (`Review`, `Native`), render as
  **unapprovable**: they are what a human should look at, and the executor will
  never run them;
- unchecking an action rewrites `approved.json` for this plan immediately.

### Dry run — do this every time

![The dry run](../artifacts/gui/dryrun.png)

**Dry-run preview** resolves each approved action into the exact operation it
would perform — quarantine destination, move destination, the link that keeps
the old path working, and any refusal with its reason — and touches nothing.
A refusal here (report-only tier, protected path, destination outside the plan's
targets) is a decision you want to see *before* the run, not after.

---

## 6. Execute

![The confirmation dialog](../artifacts/gui/confirm.png)

**Execute…** asks one itemized question: *"Run N approved actions (up to X)?"* —
every action, its size and its consequence, behind a danger-styled button. Only
that button runs anything.

![The run's results](../artifacts/gui/execute.png)

While it runs you get per-item results, and after it you get the total:

- **Done** — the operation completed and the payload was verified by digest.
- **Skipped** — the engine re-validated the item and deliberately left it alone
  (the file is locked, it is gone, the destination exists, a link could not be
  created so the move was not attempted). Skips are reported and journaled.
- **Refused** — the item should never have been executable (protected path,
  report-only tier): the engine will not do it, whatever the plan said.
- **Failed** — something went wrong with the operation itself; the report says
  what, and undo is still available.

"Delete" is **quarantine**: the payload moves to
`<volume>\_spacesage_quarantine\<plan token>\…` (`$XDG_DATA_HOME/spacesage/
quarantine` on Linux) with a `manifest.json` recording what is in there. The
bytes are still on your disk.

---

## 7. Undo (or not)

![The Undo screen](../artifacts/gui/undo.png)

The **Undo** switch beside *Plan* lists the journals this app wrote and one row
per operation. *Revert selected* or *Revert all pending* puts everything back —
link first, then the move, then the quarantine — verifying each payload against
the digest recorded on the way in. An occupied original path **blocks** the
revert (nothing is clobbered); a payload you deleted by hand is reported gone.

Reclaiming the space for good is a separate, deliberate step: SpaceSage has no
purge button in v0.1, on purpose. When you are satisfied the run was right,
empty the quarantine folder yourself — that is the moment the bytes are really
gone.

---

## Without a display (automation, servers, CI)

The app runs offscreen for smoke tests and screenshots:

```sh
QT_QPA_PLATFORM=offscreen spacesage --capture shot.png --capture-delay 1500
QT_QPA_PLATFORM=offscreen spacesage --self-check      # Qt/platform probe, exit 0
```

`--capture` renders the real window to a PNG and exits (that is how the packaged
build proves itself in CI); `--self-check` and `--version` are the other two
internal flags.

### Engine library & CLI

The analysis engine is a zero-dependency Python library, and the internal CLI
exposes every stage of it for scripting and CI (`ingest`, `stats`, `classify`,
`candidates`, `plan`, `deepscan`, `apply`, `undo`). Same rules as the app:
everything is read-only until `apply … --execute`, which resolves a dry run
first and demands an approval manifest bound to the plan's `plan_id`.

```sh
uv run python -m spacesage ingest export.csv --db index.db
uv run python -m spacesage candidates --db index.db
uv run python -m spacesage plan --db index.db --to D: -o plan.json
uv run python -m spacesage apply plan.json --approve approved.json            # dry run
uv run python -m spacesage apply plan.json --approve approved.json --execute  # really does it
uv run python -m spacesage undo spacesage.journal.jsonl
```

[`docs/design.md`](design.md) §11 and the CLI's own `--help` cover the flags; the
reading order for the engine is [design.md](design.md) →
[rules.md](rules.md) → [ai.md](ai.md).

---

## Troubleshooting

| symptom | what it means |
|---|---|
| *"Qt could not load a windowing platform"* on Linux | install `libegl1 libgl1 libglvnd0 libxkbcommon0` (the app says so in a real dialog) |
| The window opens, the export fails | the file is not a WizTree CSV: the seven-column export is the contract, and the error names the first row it could not read |
| Moves land but the old path is gone | the plan was built with links off, or the link needed elevation (Windows: run once as administrator or enable Developer Mode — the app tells you which before the move) |
| "Skipped: in use" | the file was locked; close the owning program and re-run the plan, the item is still pending |
| The quarantine folder is getting large | that is by design: purge it by hand once you trust the run |
| Settings say `AI not ready: …` | the coded reason is in the Settings (AI) result line: no key, `local_only`, unreachable endpoint, … |
| Where is my index? | Settings → *Index* names the file and the data folder; `SPACESAGE_DATA_DIR` moves it |

More questions: [faq.md](faq.md). The screen-by-screen reference is
[app-guide.md](app-guide.md).
