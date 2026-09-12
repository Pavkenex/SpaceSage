"""POSIX backend: ``shutil.move``, ``os.symlink`` and an XDG-style quarantine.

The engine is developed and tested on POSIX first (``docs/design.md`` section 8),
so this is the reference implementation of :class:`~spacesage.executor.backend.Backend`:
every primitive is a stdlib call, the one Windows-only link type (``JUNCTION``)
is emulated with a directory symlink -- which is what the platform actually
has -- and "in use" is an advisory ``flock`` probe instead of Windows' share
modes.  What a POSIX move cannot do (NTFS compression) is refused with a reason
rather than approximated.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil

from . import backend
from .backend import PrimResult


class PosixBackend:
    """The POSIX primitives (``Linux``, ``macOS``, and the dev containers)."""

    name = "posix"

    def path_for_os(self, path: str) -> str:
        """POSIX paths are already what the OS wants."""
        return path

    def can_link(self, kind: str, *, is_dir: bool) -> tuple[bool, str]:
        """``JUNCTION``/``SYMLINK`` always work here; hard links are files-only."""
        if kind == "NONE":
            return True, "no link requested"
        if kind == "HARDLINK":
            if is_dir:
                return False, "POSIX does not hard-link directories"
            return True, "same-volume hard link"
        if kind in ("JUNCTION", "SYMLINK"):
            return True, "directory symlink (junctions are Windows-only)"
        return False, f"unknown link kind {kind}"

    def can_compress(self) -> tuple[bool, str]:
        """NTFS compression is a Windows feature; nothing to emulate."""
        return False, "NTFS compression is a Windows-only feature"

    def is_locked(self, path: str) -> tuple[bool, str]:
        """Advisory ``flock`` probe: a locked payload is skipped, never forced.

        POSIX has no "in use by another process" flag, so the probe reports the
        one thing it can *prove*: another process holds a lock on the file.
        """
        if os.path.isdir(path):
            return False, ""
        try:
            handle = os.open(path, os.O_RDONLY)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY):
                return True, f"it cannot be opened for inspection ({exc.strerror})"
            return False, ""
        try:
            return _flock_probe(handle)
        finally:
            os.close(handle)

    def move(self, src: str, dest: str) -> PrimResult:
        """``shutil.move``: a rename on one volume, a copy + delete across two."""
        parent = os.path.dirname(dest)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            return PrimResult(ok=False, detail=f"cannot create {parent}: {exc}")
        cross_volume = not backend.same_volume(src, dest)
        try:
            shutil.move(src, dest)
        except OSError as exc:
            return PrimResult(ok=False, detail=f"shutil.move failed: {exc}")
        if not os.path.lexists(dest):
            return PrimResult(ok=False, detail="the move did not produce the destination")
        if os.path.lexists(src):
            return PrimResult(ok=False, detail="the move left the source behind")
        if cross_volume:
            return PrimResult(
                ok=True,
                detail="copied to the destination and removed the source (cross-volume)",
                note="the destination is on another volume, so the payload was copied",
            )
        return PrimResult(ok=True, detail="renamed (same volume)")

    def create_link(self, *, path: str, target: str, kind: str, is_dir: bool) -> PrimResult:
        """``os.symlink`` for ``JUNCTION``/``SYMLINK``, ``os.link`` for ``HARDLINK``."""
        if kind == "NONE":
            return PrimResult(ok=True, detail="no link requested")
        if kind == "HARDLINK":
            if is_dir:
                return PrimResult(ok=False, detail="POSIX does not hard-link directories")
            try:
                os.link(target, path)
            except OSError as exc:
                return PrimResult(ok=False, detail=f"os.link failed: {exc}")
            return PrimResult(ok=True, detail="hard link created (one payload, two names)")
        if kind not in ("JUNCTION", "SYMLINK"):
            return PrimResult(ok=False, detail=f"unknown link kind {kind}")
        note = None
        if kind == "JUNCTION":
            note = "junctions are Windows-only: created a directory symlink"
        try:
            os.symlink(target, path, target_is_directory=is_dir)
        except OSError as exc:
            return PrimResult(ok=False, detail=f"os.symlink failed: {exc}")
        return PrimResult(ok=True, detail=f"symlink created -> {target}", note=note)

    def remove_link(self, path: str) -> PrimResult:
        """Remove a symlink (never its target); hard links are plain files here."""
        if os.path.lexists(path) and backend.reparse_kind(path) is None:
            return PrimResult(ok=False, detail="there is no link at that path")
        try:
            os.unlink(path)
        except OSError as exc:
            return PrimResult(ok=False, detail=f"os.unlink failed: {exc}")
        return PrimResult(ok=True, detail="link removed")

    def compress(self, path: str) -> PrimResult:
        """Refused: POSIX has no in-place NTFS compression."""
        return PrimResult(ok=False, detail="NTFS compression is a Windows-only feature")

    def uncompress(self, path: str) -> PrimResult:
        """Refused: nothing was compressed on this platform."""
        return PrimResult(ok=False, detail="NTFS compression is a Windows-only feature")

    def default_quarantine_root(self, path: str) -> str:
        """Same volume, XDG-style: the home volume goes to ``$XDG_DATA_HOME``.

        Payloads on the home volume land in ``~/.local/share/spacesage/quarantine``
        (the XDG trash convention); everything else lands in
        ``<mount point>/.spacesage_quarantine-<uid>`` next to the payload, so the
        quarantine move stays a rename on the volume the bytes already occupy.
        """
        home = os.path.expanduser("~")
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        home_device = backend.device(home)
        payload_device = backend.device(path)
        if home_device is not None and home_device == payload_device:
            return os.path.join(data_home, "spacesage", "quarantine")
        return os.path.join(mount_point(path), f"{backend.POSIX_QUARANTINE_DIRNAME}-{_uid()}")


def mount_point(path: str) -> str:
    """The mount point of the volume ``path`` lives on (walk up while ``st_dev`` holds).

    Used by the default quarantine location: everything below one mount point is
    one filesystem, so a ``rename`` there cannot fail with ``EXDEV``.
    """
    current = os.path.abspath(path)
    if not os.path.lexists(current):
        current = os.path.dirname(current) or "/"
    try:
        device = os.stat(current).st_dev
    except OSError:
        return "/"
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return current
        try:
            if os.stat(parent).st_dev != device:
                return current
        except OSError:
            return current
        current = parent


def _uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else 0


def _flock_probe(handle: int) -> tuple[bool, str]:
    """``(locked, reason)``: can this process take an exclusive advisory lock?"""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows never reaches this backend
        return False, ""
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True, "another process holds a lock on it"
    with contextlib.suppress(OSError):  # releasing a lock we hold cannot fail
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False, ""


BACKEND = PosixBackend()
"""The singleton :func:`spacesage.executor.current_backend` hands out."""
