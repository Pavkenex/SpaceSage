# ADR-0002: Desktop GUI with PySide6 (Qt) as the product surface

**Status:** accepted · **Date:** 2026-09-12

## Context

Matija: the product is a program with a UI — explicitly not a web app and not a CLI. It must run on Windows (where WizTree lives), be buildable and verifiable in the Linux dev container, and ship as an executable without extra runtime setup.

## Decision

Build the app as a native desktop application with **PySide6 (Qt Widgets)**:

- mature tables/trees/dialogs — exactly this tool's UI vocabulary;
- one language across engine + UI (Python; engine reusable as a library);
- runs on Windows/macOS/Linux; PyInstaller → windowed single-file exe;
- verifiable headless in CI/dev container: `QT_QPA_PLATFORM=offscreen` + `widget.grab()` screenshots (verified working in this container — see `docs/dev-environment.md`).

## Alternatives considered

- **Tauri / Electron / Wails** (web UI in a wrapper) — rejected: user explicitly wants a program, not a web app; also heavier toolchains.
- **.NET WPF/WinUI** — Windows-only builds, new toolchain, engine is Python.
- **Avalonia (C#)** — viable, but splits the stack into two languages.
- **Tkinter/ttk** — stdlib, but dated look and weak table widgets.
- **Flet / Kivy** — less standard for desktop tooling; harder to verify headless.

## Consequences

- Qt adds packaging weight to the app (~100 MB) — confined to the `[gui]` extra; the engine stays zero-dependency.
- UI logic lives in view-models so core tests remain headless-fast; GUI smoke tests render offscreen screenshots in CI.
- Dev containers lacking system GL libraries use the vendored-libs helper (`scripts/setup-devcontainer-glibs.sh` + `scripts/gui-env.sh`).
