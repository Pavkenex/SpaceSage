# SpaceSage — FAQ

Straight answers, including the ones where the answer is "not in v0.1".

**Does SpaceSage delete my files?**
No. Every "Delete" is a **quarantine**: the payload moves into a quarantine store
on the same volume and keeps its bytes, with a manifest describing what is in
there. Undo moves it back. Emptying the store — the point at which the space is
really reclaimed — is a step you take yourself; there is no purge button in
v0.1 on purpose. See [safety.md](safety.md).

**Does it touch anything before I approve a plan?**
No. Import, ranking, planning and the dry run are read-only. The only files they
write are the app's own index database and plan documents. Execution needs an
itemized, approved plan and one confirmation dialog.

**Do I need WizTree?**
Yes for v0.1: the input is a WizTree CSV export (its *Export to CSV* feature).
SpaceSage does not scan your disk itself — it does not need to, the export
already contains your whole tree. The one exception is the optional `deepscan`
CLI verb, which walks named folders read-only to prove which same-size files are
byte-identical.

**Is the AI required?**
No. Everything — ingest, classification, ranking, planning, execution, undo —
works without a provider, and that is how the app starts. Add a provider in
Settings (AI) when you want help with the ambiguous long tail: the rows no rule
recognised.

**Which providers work?**
Any OpenAI-compatible HTTP endpoint: OpenAI, OpenRouter, OpenCode Zen, Ollama,
LM Studio, vLLM, llama.cpp's server, or a custom base URL. Presets exist for the
common ones; there is no SDK and no vendor lock-in — it is plain HTTP against
`/chat/completions`.

**Does the AI see my file names, or my files?**
It sees *facts about items*: the path (or a token), folder flag, size,
extension, age, the rule verdict the item already has. It never sees file
contents, never sees the index database, and never sees your export. The
`redact_paths` switch replaces paths with local tokens, and **local-only mode**
refuses every non-loopback endpoint before a socket is opened. Details in
[ai.md](ai.md).

**Can the AI delete something?**
No. AI output becomes a suggestion, an annotation on a plan, or a rule proposal
you accept. It cannot execute, cannot approve, and cannot add an action to a
plan — the plan's action list is only ever written by the rule engine.

**Windows only?**
Windows is the primary target (junctions, `robocopy`, `compact.exe`, elevation
handling), and the release ships a `spacesage.exe` plus a Linux bundle. Running
from source works on Linux and macOS; POSIX execution uses symlinks and
`shutil.move` (and refuses NTFS compression with a reason instead of
approximating it).

**Do I need administrator rights?**
For the app itself, no. Analysis and quarantine work as your user. Windows
*junctions* need no elevation, but a **file symlink** needs either elevation or
Developer Mode — and when neither is available the engine skips that action
*before* the move (so a path can never be left dangling) and tells you why.

**Where does the app keep its files?**
The per-user data directory: `%LOCALAPPDATA%\spacesage` on Windows,
`~/.local/share/spacesage` on Linux, `~/Library/Application Support/spacesage`
on macOS — `SPACESAGE_DATA_DIR` overrides it. Settings → *Index* names the index
file, its size and the folder. Deleting that folder resets the app completely
(the index, the plans, the AI cache); your actual files are untouched.

**Can I run it against an export from another machine?**
Yes, as analysis. The paths in the export are read as exported, so the app can
rank and plan for a machine it is not running on — but only moves need a
destination that exists locally, and the dry run is where you find out.

**Why does *Est. gain* sometimes say "up to N MiB"?**
Because that number is an upper bound the app has not verified. Weak duplicate
clusters are the usual case: same file name **and** same size, which is evidence,
not proof. The verified version of that claim comes from the `deepscan` CLI verb,
which hashes the candidates for real. An unverified bound is never presented as
a fact.

**Why is there a row that says "No action"?**
Because leaving something alone is a decision worth showing. A "no action" row
carries the reason ("this cache is huge but rebuilding it costs more than the
space buys"), and it is never executable. Rules you have not written and entries
no rule matched appear as **undecided** instead, carrying no suggestion at all.

**What happens to locked files?**
They are skipped, with "in use" as the reason, and they stay pending. The engine
probes for exclusive access before touching anything; it never yanks a file out
from under a running program. Close the program and run the plan again.

**How long does an import take?**
The indexer is built for 1–20 million rows and targets at least a million rows
per minute on a laptop with flat memory use. A typical full-disk export is a few
minutes; the status bar counts rows per second while it runs, and the window
stays responsive because the engine never runs on the UI thread.

**Can I automate it?**
Yes — the engine is a zero-dependency library with an internal CLI for every
stage (`ingest`, `stats`, `classify`, `candidates`, `plan`, `deepscan`, `apply`,
`undo`), JSON output on the stages that matter, and the same dry-run-first,
manifest-gated execution rules as the app. The desktop app stays the product
surface; the CLI is the automation surface.

**Is it open source?**
MIT licensed. The engine is stdlib-only by design so it stays runnable anywhere,
including headless machines; the desktop app adds exactly one dependency
(PySide6).

**What is *not* in v0.1?**
Purging the quarantine; a live scan (no WizTree needed); near-duplicate image
and video similarity; decision memory that learns your accept/reject
preferences; scheduled rescans with alerts; a macOS build artifact. The engine
has the seams for most of these (design §15), and none of them change the safety
contract above.

**The app says there are *warnings* on my plan. Should I panic?**
No — a warning is something the planner wants you to know before running:
a destination drive it cannot measure, an action that will need elevation, a
move whose link cannot be created. Zero warnings means nothing in the plan needs
your attention beyond the approval itself.

**How are rows ranked, exactly?**
`bytes × tier weight × confidence × recency`, computed from the rule verdict —
so a rank can be re-derived by hand, and the details pane shows the factors it
used (including the gain basis: full size, verified duplicates, …). Bigger,
safer, more certain and more recent wins first.
