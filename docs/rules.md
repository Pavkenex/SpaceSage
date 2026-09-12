# Writing rule packs

SpaceSage classifies every entry of a WizTree export with **rule packs**: TOML
files holding an ordered list of rules. Each rule answers one question — *what
is this, how risky is it, and what should happen to it?* — with a category, a
risk tier, an action, a confidence and a plain-language rationale.

The classifier is `spacesage/rules.py`; the built-in packs live in
`spacesage/rules/`; your own packs live in the user rules directory.

```
spacesage classify --list-rules              # the effective rule order, with matchers
spacesage classify --db index.db             # per-category / per-tier report
spacesage classify --db index.db --json      # the same report as JSON
spacesage classify --db index.db --materialize   # also write the categories table
spacesage classify --db index.db --rules DIR # load your packs from DIR instead
```

## Where packs live

| Location | Purpose |
|---|---|
| `spacesage/rules/*.toml` | built-in packs shipped with the package (read-only) |
| `~/.config/spacesage/rules/*.toml` | your packs (Linux/macOS) |
| `%APPDATA%\spacesage\rules\*.toml` | your packs (Windows) |
| `$SPACESAGE_RULES_DIR` | overrides the user directory (tests, portable installs) |

Pack priority is the pair **(order, pack id)**; `[pack] order` defaults to
`100`, and the built-ins use 10–90:

| pack | order | covers |
|---|---|---|
| `windows.toml` | 10 | OS scratch space, servicing stores, hibernation/pagefile, Recycle Bin |
| `dev.toml` | 20 | package-manager caches, project artifacts, model caches, container disks |
| `browsers.toml` | 30 | Chrome / Edge / Brave / Firefox profile caches |
| `games.toml` | 40 | Steam / Epic / GOG / Battle.net libraries, shader caches, redistributables |
| `installers.toml` | 50 | downloaded installers and partial downloads |
| `misc.toml` | 60 | temp files, logs, backups, cloud-sync folders, catch-alls |
| `media.toml` | 90 | personal media as MOVE candidates (never shadows the packs above) |

**User packs are tried before built-in packs**, and a user rule with the same
`id` as a built-in rule *shadows* it: the built-in is dropped and yours takes
its place in the order. That gives you two moves:

* **shadow a built-in rule** to change its category, tier, action or patterns;
* **add a rule in front of the built-ins** to carve an exception out of a
  broad built-in pattern (e.g. protect one project's `node_modules`).

Rule ids must be unique across your packs — two user packs defining the same id
is an error, because there would be no way to tell which one you meant.

## A rule

```toml
[pack]
id = "my-pack"          # optional (defaults to the file name); lowercase
title = "My rules"      # optional
order = 5               # optional, default 100

[[rule]]
id = "old-camera-raws"          # required, unique, lowercase
path = ["**/Users/*/Camera/**"] # matcher: glob list (any of them may match)
ext = ["cr2", "nef"]            # matcher: extension list (files only)
min_size = "512 MiB"            # matcher: size floor (bytes or "10 MiB")
older_than_days = 365           # matcher: age floor (needs a known timestamp)
name_regex = "^IMG_"            # matcher: regular expression on the name
category = "media-photos"       # required: what this is
tier = "T2"                     # required: T1 | T2 | T3
action = "MOVE"                 # required: see the action table
confidence = 0.6                # required: 0.0 – 1.0
rationale = "Camera raw files; move them to the archive drive."  # required
native = "..."                  # optional: the tool that does this better
```

A rule needs **at least one matcher**. Every matcher you give must hold
(they are AND-ed); within `path` and `ext` the lists are OR-ed.
Unknown keys are an error — a typo like `pat = [...]` fails loudly instead of
silently matching everything.

### `path` globs

* `*` and `?` match inside one path component and never cross a separator.
* `**` crosses separators. A leading `**/` matches zero or more components,
  so `**/Temp/**` covers `C:\Temp` and `C:\Temp\a\b`.
* A trailing `/**` also matches the folder itself — that is how a folder row
  and everything below it get the same category.
* Both separators are accepted (`\` and `/`), in patterns and in paths.
* **Windows paths match case-insensitively** (drive letter or UNC prefix —
  Windows filenames are case-insensitive); POSIX-style paths match
  case-sensitively.
* Patterns must not end with a separator: write `**/Temp/**`, not `**/Temp/`.
* `[abc]` character classes and `[!abc]` negations work as in `fnmatch`.

### The other matchers

| matcher | matches | notes |
|---|---|---|
| `ext` | the file extension, without the dot | folders never match (their extension is `NULL`); case-insensitive |
| `min_size` | entries at least this large | `"10 MiB"`, `"500 KB"`, `"1.5G"` — all suffixes are binary; folders compare their file-row subtree size |
| `older_than_days` | entries at least this old | an entry without an export timestamp never matches — unknown age is not "old" |
| `name_regex` | `re.search` on the last path component | case-insensitive for Windows-style paths |

### Tiers and the action vocabulary

| tier | meaning |
|---|---|
| `T1` | disposable scratch data: temp files, download caches, browser caches, shader caches |
| `T2` | regenerable but meaningful: package stores, project artifacts, media moves, installers |
| `T3` | report-only: system data, cloud folders, backups, anything ambiguous |

| action | meaning |
|---|---|
| `DELETE_QUARANTINE` | remove to the quarantine store (undoable); never a straight delete |
| `MOVE` | relocate the folder/file to another drive (with a link back when needed) |
| `COMPRESS_NTFS` | NTFS compression candidate |
| `REVIEW` | look at it yourself; no automatic suggestion |
| `NATIVE` | the owning tool frees this space safely — `native` is **required** and shown in the report |
| `KEEP` | explicit *No action* with the reason: this data is fine where it is |

Rules the user has not written yet, and entries no rule matched, fall back to
the `unknown` classification: tier `T3`, action `REVIEW`, confidence `0.0`.
That fallback is never destructive — and it is the report section to watch when
you are tuning a pack.

## How the numbers are computed

* Classification is **per entry**, first match wins, in the effective order
  (user packs, then built-ins) — so order is the tool you tune with.
* Folder rows classify with the **same rule semantics as files**; `min_size`
  and the report's folder column use the folder's subtree size computed from
  **file rows only** (the engine never sums a folder row together with its
  children), and `older_than_days` uses the folder row's own `Modified` value.
* Category sizes in the report are the **file rows** classified into that
  category. The `(+X in folders)` figure is the subtree size of the folder
  rows that matched — context, not a second total.
* `--materialize` writes one row per entry into the `categories` table
  (schema v3): `entry_id, pack, rule_id, category, tier, action, confidence,
  rationale, native, bytes, is_dir`, plus a fingerprint of the effective rules
  in `meta` (`classify.rules_sha256`). Plain `classify` never writes.

## Testing a pack

```sh
# structural check: does every pack parse, and what is the effective order?
spacesage classify --list-rules

# behaviour: classify an export and look at the unknown bucket
spacesage classify --db index.db --top 50
```

The test suite has the same two layers: `tests/test_rules.py::test_builtin_packs_load_validate_and_have_unique_ids`
lints the built-ins (they all load, all ids are unique, every `NATIVE` rule has
a command, every rationale is a sentence), and
`tests/test_rules.py::EXPECTED` pins the classification of a synthetic tree
(`tests/fixtures/data/rule_packs.csv`) path by path. Add a row there when you
add a rule that matters.

## Gotchas

* `min_size` on a folder uses the subtree, so a rule with both a folder-depth
  glob and a size floor can match the folder while its files match something
  more specific. That is intended: the folder is the move candidate, the files
  are the contents.
* Age filters need a timestamp: a folder row exported without `Modified`, or a
  file whose timestamp cell was empty, never matches `older_than_days`.
* Deleting a specific folder (`**/node_modules/**`) also matches its children
  individually. If you want the folder to be the only candidate, say so with a
  pattern that does not cover the contents.
* `KEEP` is not "ignore": it is a positive statement in the report. Use it when
  a whole class of entries should visibly be a deliberate "No action" (system
  directories, installed programs) instead of showing up as `unknown`.
