"""Windows backend: ``robocopy``, ``mklink``, ``compact`` and long paths.

The Windows backend is the product's real target (``docs/design.md`` section 8):
moves go through ``robocopy /MOVE /E /COPYALL`` so ownership, ACLs and
timestamps survive, ``shutil.move`` is the fallback when robocopy is missing or
refuses; directory links are ``mklink /J`` junctions (no elevation needed),
file links are symlinks (elevation or Developer Mode required -- the executor
*refuses the whole move* rather than leaving a path dangling), hard links are
``os.link``; quarantine is a same-volume rename into
``<volume root>\\_spacesage_quarantine``.  "In use" is detected by an exclusive
open (and, for directories, a rename-to-self probe), and NTFS compression uses
``compact.exe``.

Everything here is import-safe on POSIX -- the module is imported on every
platform for its pure helpers (``longpath``, the elevation probes) -- and every
Windows-only call is guarded so a POSIX host with Windows-shaped paths can still
be tested.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from typing import Any

from . import backend
from .backend import PrimResult

MAX_PATH_SAFE = 240
"""Above this length a path is handed to the OS with the ``\\\\?\\`` prefix."""

ROBOCOPY_OK = 8
"""``robocopy`` exit codes below this are success (0 = nothing to do, 1 = copied)."""

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
"""Keep the console window out of the user's face when running ``cmd``/``compact``."""


class WinBackend:
    """The Windows primitives (``robocopy``, ``mklink``, ``compact``)."""

    name = "win"

    def path_for_os(self, path: str) -> str:
        """The path as the OS should see it (long paths get the ``\\\\?\\`` prefix)."""
        return longpath(path)

    def can_link(self, kind: str, *, is_dir: bool) -> tuple[bool, str]:
        """What this process may create right now (elevation / Developer Mode)."""
        if kind == "NONE":
            return True, "no link requested"
        if kind == "JUNCTION":
            if not is_dir:
                return False, "a junction is a directory link; files get a symlink or hard link"
            return True, "junctions need no elevation"
        if kind == "SYMLINK":
            return can_create_symlinks()
        if kind == "HARDLINK":
            if is_dir:
                return False, "Windows does not hard-link directories"
            return True, "hard links need no elevation"
        return False, f"unknown link kind {kind}"

    def can_compress(self) -> tuple[bool, str]:
        """``compact.exe`` is part of every Windows install -- and only exists there."""
        if os.name != "nt":  # pragma: no cover - the win CI job covers it
            return False, "the Windows backend is not running on Windows"
        return True, "NTFS compression via compact.exe"

    def is_locked(self, path: str) -> tuple[bool, str]:
        """Exclusive-open probe (design section 8): locked payloads are skipped.

        A file that cannot be opened for writing is in use; a directory proves
        the same thing by refusing a rename to itself (Windows checks delete
        access, which is what the move needs).  Best effort by construction --
        a probe that cannot tell reports "not locked" and lets the operation
        itself fail loudly.
        """
        if os.name != "nt":  # pragma: no cover - only reachable from the win job
            return False, ""
        target = longpath(path)
        if os.path.isdir(target):
            try:
                os.rename(target, target)
            except PermissionError as exc:
                return True, _locked_reason(exc)
            except OSError:
                return False, ""
            return False, ""
        if os.access(target, os.W_OK):
            try:
                handle = os.open(target, os.O_RDWR | getattr(os, "O_BINARY", 0))
            except PermissionError as exc:
                return True, _locked_reason(exc)
            except OSError:
                return False, ""
            else:
                os.close(handle)
        return False, ""

    def move(self, src: str, dest: str) -> PrimResult:
        """``robocopy /MOVE`` with ``shutil.move`` as the fallback (ACL-preserving)."""
        parent = os.path.dirname(dest)
        try:
            if parent:
                os.makedirs(longpath(parent), exist_ok=True)
        except OSError as exc:
            return PrimResult(ok=False, detail=f"cannot create {parent}: {exc}")
        if os.path.lexists(dest):
            return PrimResult(ok=False, detail="the destination already exists")
        if os.path.isdir(src) and not os.listdir(src):
            # robocopy copies files; an empty directory would be left behind.
            return self._shutil_move(src, dest, note="empty directory: renamed directly")
        if os.path.isdir(src) or os.path.basename(src) == os.path.basename(dest):
            first = self._robocopy_move(src, dest, "/COPYALL")
            if first.ok:
                return first
            second = self._robocopy_move(src, dest, "/COPY:DAT")
            if second.ok:
                return PrimResult(
                    ok=True,
                    detail=second.detail,
                    command=second.command,
                    note=(
                        "robocopy /COPYALL was refused (ACLs need backup rights); "
                        "retried with /COPY:DAT"
                    ),
                )
            failure = f"robocopy could not do it ({second.detail})"
        else:
            failure = "robocopy can only move a file under its own name"
        fallback = self._shutil_move(src, dest)
        if fallback.ok:
            return PrimResult(
                ok=True,
                detail=fallback.detail,
                note=f"{failure}; moved with shutil.move instead",
                fallback=True,
            )
        return PrimResult(ok=False, detail=fallback.detail or failure)

    def _robocopy_move(self, src: str, dest: str, copying: str) -> PrimResult:
        """One ``robocopy`` attempt (returns ``ok=False`` for the caller to fall back)."""
        if os.path.isdir(src):
            args = (src, dest, "/MOVE", "/E", copying)
        else:
            args = (
                os.path.dirname(src) or ".",
                os.path.dirname(dest) or ".",
                os.path.basename(src),
                "/MOV",
                copying,
            )
        command = ("robocopy", *args, "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
        code, output = _run(command)
        if code < ROBOCOPY_OK:
            return PrimResult(ok=True, detail=f"robocopy moved it (exit {code})", command=command)
        return PrimResult(
            ok=False,
            detail=_short(output) or f"robocopy exit code {code}",
            command=command,
        )

    def _shutil_move(self, src: str, dest: str, *, note: str | None = None) -> PrimResult:
        """The fallback move (never nests ``src`` inside an existing ``dest``)."""
        if os.path.lexists(dest):
            return PrimResult(ok=False, detail="the destination already exists")
        cross_volume = not backend.same_volume(src, dest)
        try:
            shutil.move(longpath(src), longpath(dest))
        except OSError as exc:
            return PrimResult(ok=False, detail=f"shutil.move failed: {exc}")
        if not os.path.lexists(dest) or os.path.lexists(src):
            return PrimResult(ok=False, detail="the move did not complete")
        if cross_volume:
            note = note or "the destination is on another volume, so the payload was copied"
            return PrimResult(
                ok=True, detail="copied to the destination and removed the source", note=note
            )
        return PrimResult(ok=True, detail="renamed (same volume)", note=note)

    def create_link(self, *, path: str, target: str, kind: str, is_dir: bool) -> PrimResult:
        """``mklink`` for junctions and symlinks, ``os.link`` for hard links."""
        if kind == "NONE":
            return PrimResult(ok=True, detail="no link requested")
        if kind == "HARDLINK":
            if is_dir:
                return PrimResult(ok=False, detail="Windows does not hard-link directories")
            try:
                os.link(longpath(target), longpath(path))
            except OSError as exc:
                return PrimResult(ok=False, detail=f"cannot create the hard link: {exc}")
            return PrimResult(ok=True, detail="hard link created (no elevation needed)")
        if kind == "JUNCTION":
            if not is_dir:
                return PrimResult(
                    ok=False,
                    detail="a junction is a directory link; files get a symlink or hard link",
                )
            return self._mklink(path, target, "/J", is_dir=True)
        if kind == "SYMLINK":
            allowed, reason = can_create_symlinks()
            if not allowed:
                return PrimResult(ok=False, detail=reason)
            return self._mklink(path, target, "/D" if is_dir else "", is_dir=is_dir)
        return PrimResult(ok=False, detail=f"unknown link kind {kind}")

    def _mklink(self, path: str, target: str, flag: str, *, is_dir: bool) -> PrimResult:
        """``mklink`` with the destination path always as the first operand."""
        args = [
            arg for arg in ("cmd", "/c", "mklink", flag, longpath(path), longpath(target)) if arg
        ]
        command = tuple(args)
        code, output = _run(command)
        if code == 0:
            return PrimResult(
                ok=True, detail=f"created with mklink {flag}".strip(), command=command
            )
        fallback = _fallback_symlink(path, target, is_dir=is_dir)
        if fallback.ok:
            return PrimResult(
                ok=True,
                detail=fallback.detail,
                command=command,
                note=f"mklink failed ({_short(output) or code}); used os.symlink instead",
                fallback=True,
            )
        return PrimResult(
            ok=False,
            detail=_short(output) or fallback.detail or f"mklink exit code {code}",
            command=command,
        )

    def remove_link(self, path: str) -> PrimResult:
        """Remove a junction/symlink itself, or one name of a hard-linked file."""
        kind = backend.reparse_kind(longpath(path))
        if kind is None:
            if not os.path.lexists(path):
                return PrimResult(ok=False, detail="there is no link at that path")
            try:
                links = os.stat(longpath(path)).st_nlink
            except OSError as exc:
                return PrimResult(ok=False, detail=f"cannot inspect the path: {exc}")
            if links < 2:
                return PrimResult(ok=False, detail="the path is a plain file, not a link")
            try:
                os.unlink(longpath(path))
            except OSError as exc:
                return PrimResult(ok=False, detail=f"cannot remove the hard link: {exc}")
            return PrimResult(
                ok=True, detail="removed one hard link (the payload stays at its other name)"
            )
        try:
            if os.path.isdir(path):
                # RemoveDirectory on a junction/symlink-to-directory drops the
                # reparse point; the target's contents stay untouched.
                os.rmdir(longpath(path))
            else:
                os.unlink(longpath(path))
        except OSError as exc:
            return PrimResult(ok=False, detail=f"cannot remove the {kind}: {exc}")
        return PrimResult(ok=True, detail=f"removed the {kind}")

    def compress(self, path: str) -> PrimResult:
        """``compact /c`` (in place, no elevation required)."""
        if not os.path.lexists(path):
            return PrimResult(ok=False, detail="the path is gone")
        command = _compact_command(path, "/c")
        code, output = _run(command, input_="y\ny\n")
        if code != 0:
            return PrimResult(
                ok=False,
                detail=_short(output) or f"compact exit code {code}",
                command=command,
            )
        if not backend.is_compressed(path):
            return PrimResult(
                ok=False,
                detail="compact reported success but the compression attribute is not set",
                command=command,
            )
        return PrimResult(ok=True, detail="compressed in place (NTFS)", command=command)

    def uncompress(self, path: str) -> PrimResult:
        """Undo :meth:`compress` (``compact /u``)."""
        if not os.path.lexists(path):
            return PrimResult(ok=False, detail="the path is gone")
        command = _compact_command(path, "/u")
        code, output = _run(command, input_="y\ny\n")
        if code != 0:
            return PrimResult(
                ok=False,
                detail=_short(output) or f"compact exit code {code}",
                command=command,
            )
        if backend.is_compressed(path):
            return PrimResult(
                ok=False,
                detail="compact reported success but the compression attribute is still set",
                command=command,
            )
        return PrimResult(ok=True, detail="compression removed (NTFS)", command=command)

    def default_quarantine_root(self, path: str) -> str:
        """``<volume root>\\_spacesage_quarantine``: same volume, so a rename."""
        return backend.join_path(backend.volume_root(path), backend.QUARANTINE_DIRNAME)


def longpath(path: str) -> str:
    """``\\\\?\\``-prefix a path once it gets close to ``MAX_PATH`` (design section 8).

    The prefix turns off path normalization, so it is added only when the path
    is long enough to need it (and never to an already-prefixed path).
    """
    if path.startswith("\\\\?\\"):
        return path
    normalized = path.replace("/", "\\")
    if len(normalized) < MAX_PATH_SAFE:
        return path
    if normalized.startswith("\\\\"):
        return "\\\\?\\UNC\\" + normalized[2:]
    return "\\\\?\\" + normalized


def is_elevated() -> bool:
    """Is this process running with administrator rights?"""
    if os.name != "nt":  # pragma: no cover - the win CI job covers it
        return False
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:  # pragma: no cover - defensive
        return False
    try:
        return bool(windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - defensive: never break on a probe
        return False


def developer_mode_enabled() -> bool:
    """Is Windows Developer Mode on (symlinks without elevation)?"""
    if os.name != "nt":  # pragma: no cover - the win CI job covers it
        return False
    try:
        winreg = _winreg_module()
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock",
        ) as handle:
            value, _kind = winreg.QueryValueEx(handle, "AllowDevelopmentWithoutDevLicense")
    except OSError:
        return False
    except Exception:  # pragma: no cover - defensive: a probe must not raise
        return False
    return bool(value)


def can_create_symlinks() -> tuple[bool, str]:
    """``(ok, reason)``: elevation or Developer Mode, exactly as design section 8 says."""
    if os.name != "nt":  # pragma: no cover - the win CI job covers it
        return False, "the Windows backend is not running on Windows"
    if is_elevated():
        return True, "the process is elevated"
    if developer_mode_enabled():
        return True, "Windows Developer Mode is enabled"
    return False, "creating symlinks needs an elevated process or Windows Developer Mode"


def _winreg_module() -> Any:
    """``winreg`` by name so type checking on a non-Windows host stays quiet."""
    return importlib.import_module("winreg")


def _fallback_symlink(path: str, target: str, *, is_dir: bool) -> PrimResult:
    """Last resort when ``mklink`` cannot run at all."""
    try:
        os.symlink(longpath(target), longpath(path), target_is_directory=is_dir)
    except OSError as exc:
        return PrimResult(ok=False, detail=f"os.symlink failed: {exc}")
    return PrimResult(ok=True, detail="symlink created with os.symlink")


def _compact_command(path: str, flag: str) -> tuple[str, ...]:
    """``compact /c`` for a file, ``compact /c /s:<dir>`` for a directory."""
    if os.path.isdir(path):
        return ("compact", flag, f"/s:{longpath(path)}", "/i")
    return ("compact", flag, longpath(path), "/i")


def _run(command: tuple[str, ...], *, input_: str | None = None) -> tuple[int, str]:
    """Run a system tool, never raising: ``(exit code, combined output)``."""
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            input=input_,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        return 1, f"cannot run {command[0]}: {exc}"
    return completed.returncode, ((completed.stdout or "") + (completed.stderr or "")).strip()


def _short(text: str, limit: int = 200) -> str:
    """First useful line of a tool's output (reports and journals stay readable)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


def _locked_reason(exc: OSError) -> str:
    message = getattr(exc, "strerror", None) or str(exc)
    return f"it is locked by another process ({message})"


BACKEND = WinBackend()
"""The singleton :func:`spacesage.executor.current_backend` hands out."""
