# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is 0, a minor bump may change behaviour: the plan schema
and the approval manifest are versioned separately and refuse versions they do
not understand.

## [Unreleased]

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

### Notes

- The packaged artifacts are not committed: CI builds them, the release workflow
  publishes them.
