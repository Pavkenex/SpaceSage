# Development environment notes

## This container (Linux aarch64, no display)

The dev container is minimal: it has **no libEGL/libglvnd** system libraries, which PySide6 (Qt) needs to load — even for offscreen rendering. Two scripts handle this:

- `scripts/setup-devcontainer-glibs.sh` — one-time: downloads `libegl1` + `libglvnd0` (arm64) from Debian and extracts them to `/opt/data/syslibs`. Idempotent; no root needed. (Only needed in this container — normal dev machines and GitHub runners have these libraries.)
- `scripts/gui-env.sh` — **source before any GUI/test run**: adds the vendored libs to `LD_LIBRARY_PATH` and, when no display is present, sets `QT_QPA_PLATFORM=offscreen`.

Typical flow here:

```sh
bash scripts/setup-devcontainer-glibs.sh   # once
uv venv .venv
uv pip install -e '.[dev,gui]'
source scripts/gui-env.sh
uv run pytest
```

Tests capture screenshots with `widget.grab()` — real renders, no display required.
