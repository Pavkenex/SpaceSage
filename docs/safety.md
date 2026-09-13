# SpaceSage — safety model

What SpaceSage will and will not do to your disk, in plain language and then in
detail. The app's rail footer says the short version on every screen:

> Analysis is read-only. SpaceSage only acts on a plan you approve.

This page is the long version: the tiers, the guardrails, and exactly what never
happens without confirmation. The design document's §2 and §8 are the same
contract for the engine's internals.

---

## The five promises

1. **Analysis never writes.** Import, classification, ranking, planning and the
   dry run are read-only. No file is created, moved or deleted by any of them;
   the only things they write are the app's own index database and its plan
   documents.
2. **Nothing executes without an itemized, approved plan.** The engine takes a
   `plan.json` plus an approval manifest bound to that plan's `plan_id`. A
   manifest for a different plan, or one naming an action id the plan does not
   contain, is refused **before a single operation is resolved** — there is no
   partial run and no "repair" path.
3. **"Delete" always means quarantine.** Payloads move into a quarantine store
   on the same volume, with a manifest describing what is in there. The bytes
   are still on disk. SpaceSage v0.1 has **no purge step at all**; emptying the
   store is a deliberate act you take yourself.
4. **Everything is journaled, and undo is real.** Each operation is written to
   an append-only JSONL journal in two phases — before the primitive runs and
   after it — with a digest of the payload. Undo reverses operations in the only
   safe order and verifies every payload against that digest.
5. **No silent partial success.** Every action reports for itself: done,
   skipped (with the reason), refused, or failed. A locked file is never yanked,
   and a failure is never rounded up to "mostly worked".

---

## Risk tiers

Every rule assigns a tier. The tier is what decides whether an action may exist
at all.

| tier | meaning | what the app does with it |
|---|---|---|
| **T1** | disposable scratch data: temp files, download caches, browser caches, shader caches | may be an executable action after you approve it — and it still only quarantines |
| **T2** | regenerable but meaningful: package stores, project artifacts, installers, media moves | may be an executable action; the planning order puts T1 before T2, and the details pane is worth reading first |
| **T3** | report-only: system data, cloud folders, backups, repositories, databases, anything ambiguous | **never executable.** T3 paths stay review items. The executor refuses them even if a plan asks |

Entries no rule matched and rules you have not written yet fall back to the
`unknown` classification: tier **T3**, action `REVIEW`, confidence `0.0`. The
fallback is never destructive, which also means an empty rule pack is a safe
state rather than a dangerous one.

## What never happens without confirmation

Concretely, per operation, the engine refuses or skips — and says so — when:

- **the path is not absolute**, or contains a **wildcard**. There is no glob
  expansion anywhere in the executor.
- **the path is a drive root, a system directory, a profile root, the home
  directory, or the quarantine store itself.**
- **the entry carries the report-only tier** (T3), whatever the plan said.
- **the source is gone or locked** ("in use" is a skip with a reason, never a
  forced move).
- **the destination already exists** — nothing is overwritten.
- **the path has become a reparse point** (a symlink or junction that appeared
  after the plan was written).
- **a move would cross volumes for a hard link**, or a link could not be created
  at all: on Windows, a file symlink needs elevation or Developer Mode, so the
  **whole action is skipped before the move** rather than leaving a path
  dangling.
- **the destination is outside the plan's target drives**, or the free-space
  budget (`free − reserve`) does not cover the payload.

The plan's own manifest decides *which ids* may run; re-validation decides
*whether each one still should*. Both have to agree.

## Guardrails inside a run

- **Re-validation is a second look, per operation, immediately before it runs.**
  The plan was written from an export; the disk may have changed since.
- **Verification is a digest, not a hope.** Every payload is hashed before and
  after the operation (`tree_sha256` over every entry, plus a full content hash
  for payloads up to 256 MiB — larger ones are reported as "unverified (payload
  too large to hash)" instead of pretending). A move that arrives with different
  bytes, or leaves its source behind, is a *failure*, and the report says where
  the payload is now so it can be moved back.
- **The journal is written in two phases.** The `start` record (source,
  destination, pre-move digest) is on disk — fsynced — before the primitive
  runs; the `end` record follows it. A start without an end means "may or may
  not have happened", and undo settles that against the live filesystem instead
  of guessing. That is what makes an interrupted run recoverable.
- **Undo verifies and never clobbers.** The link is removed before the move it
  was created for, the move is reversed before the quarantine it followed. An
  original path that is occupied again is **blocked** and stays pending for the
  next run. Undo is itself journaled, so running it twice says "nothing to undo"
  rather than moving things back and forth.
- **Nothing is created outside a plan's own workspace and the quarantine
  store.** The app's own files live in the per-user data directory (Settings →
  *Index* names them).

## Where quarantined data lives

| platform | location |
|---|---|
| Windows | `<volume>:\_spacesage_quarantine\<plan token>\<volume label>\<path components>` |
| Linux (home volume) | `$XDG_DATA_HOME/spacesage/quarantine/<plan token>/…` |
| Linux (other volume) | `<mount point>/.spacesage_quarantine-<uid>/<plan token>/…` |

The plan token is the first 16 hex characters of the plan's `sha256`; the full id
is in the store's `manifest.json` and in every journal record. Windows puts the
store on the source's own volume so a quarantine stays a rename (fast, atomic,
same filesystem); POSIX follows the XDG convention. `--quarantine DIR` overrides
both for the CLI.

**Purge is a separate decision.** Quarantining frees *space* only in the sense
that the bytes are out of the way: to actually reclaim them you empty the store
yourself once you are satisfied. SpaceSage will not do it for you, and there is
no flag that does.

## The AI layer

The AI is optional, unprivileged, and structurally unable to touch your disk.

- **It can suggest, classify, explain and review. It can never execute.** An AI
  answer becomes a suggestion, an annotation on a plan, or a rule proposal — and
  a rule is a *rule*, which the deterministic engine then applies like any
  other. Plan action lists are only ever written by the rule engine.
- **What leaves the machine is facts about items, never contents.** The path (or
  a token), folder flag, size, extension, age, the rule verdict it already has,
  and for grouped rows the member paths. Never your file contents, never the
  index database, never the export.
- **`redact_paths`** replaces every path with a stable local token before the
  payload is built. The mapping stays on your machine, and the advice gets
  worse — `node_modules` carries signal a token does not.
- **Local-only mode** (`local_only`) refuses every non-loopback endpoint
  *before a socket is opened*: no request, not even a DNS lookup. Ollama and LM
  Studio satisfy it unchanged; a cloud provider configured while it is on fails
  loudly with a coded error rather than leaking.
- **Prompt injection is treated as data.** Item data goes into an
  `<item-data>` block with angle brackets escaped, so a file named
  `ignore-previous-instructions.txt` is a file name, not an instruction.
- **Every failure is coded and visible.** Unreachable provider, refused
  request, missing key, `local_only`, timeout — the code and its hint render in
  the AI card; the list, the plan and your session survive it.
- **Apply as rule is the only door.** An AI verdict can become a rule file, and
  only after you have seen the exact TOML, a dry run of it, and confirmed.
  Tiers the executor may not touch are refused with the engine's own sentence.

## What SpaceSage does not do

- It does not run uninstallers, edit the registry, change services, or apply
  "native fixes" itself. Native-tool advice (a Steam library move, WSL/Docker
  compaction, DISM, Storage Sense) is **advice**: a sentence telling you what
  the owning tool should do.
- It does not scan your disk in v0.1 — it reads the export you give it. (The
  optional `deepscan` CLI verb walks live folders read-only, to prove duplicate
  groups.)
- It does not hide rows it decided against. A "no action" row renders with its
  reason; an undecided row renders as undecided.
- It does not phone home, check for updates, or send telemetry. The only network
  traffic it can produce is the AI provider you configured.

## If something goes wrong

1. **Open Undo** (the switch beside *Plan*). Every operation of every run is
   listed with its verification status; revert all or a selection.
2. **Read the journal.** It is JSONL next to the plan workspace: each record
   carries the operation, the paths, the digests and the outcome — including the
   ops that were skipped and why.
3. **A blocked revert** (the original path is occupied) stays pending; move the
   occupying file away and revert again. Nothing was clobbered in the meantime.
4. **Quarantined payloads are ordinary files** in the store's folder; you can
   always copy one out by hand, and the store's `manifest.json` tells you what
   each one is.
