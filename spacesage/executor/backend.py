"""Platform layer of the executor: digests, guards and the backend contract.

The executor (``docs/design.md`` section 8) is split so that the dangerous part
stays small and reviewable:

* this module -- everything platform independent: the **digests** that prove a
  payload arrived intact, the **guards** that refuse paths nobody should ever
  touch, the ``PrimResult`` a primitive reports, and the ``Backend`` protocol
  every platform implements;
* :mod:`spacesage.executor.posix` / :mod:`spacesage.executor.win` -- the
  primitives themselves (``shutil``/``os.symlink`` vs
  ``robocopy``/``mklink``/``compact``);
* :mod:`spacesage.executor.journal` -- the append-only JSONL journal and the
  undo engine;
* :mod:`spacesage.executor` -- the dispatcher: manifest validation, per-op
  re-validation against the live filesystem, execution and reporting.

Nothing in this module mutates anything: it only inspects (``lstat``, reads for
the hashes) and decides.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DEFAULT_CONTENT_LIMIT = 256 * 1024 * 1024
"""Payloads at or below this size get a content hash on top of the tree hash.

Above it the digest still covers every path, size and link target (cheap), but
the bytes are not read: verification of a huge payload reports ``unavailable``
instead of pretending.
"""

HASH_CHUNK = 1024 * 1024
"""Read size of the streaming content hashes."""

QUARANTINE_DIRNAME = "_spacesage_quarantine"
"""Quarantine directory created on the source's own volume (Windows naming)."""

POSIX_QUARANTINE_DIRNAME = ".spacesage_quarantine"
"""Per-volume quarantine directory on POSIX (the XDG-trash convention)."""

PLAN_TOKEN_LENGTH = 16
"""Hex characters of the plan id used as the quarantine token.

The full id is recorded in the audit manifest and in every journal record; the
directory name is shortened because ``sha256:`` is not a legal path component on
Windows and deep trees must survive ``MAX_PATH``.
"""

PLAN_TOKEN_RE = re.compile(r"^sha256:([0-9a-f]{64})$")

REPARSE_TAG_MOUNT_POINT = 0xA0000003
"""``IO_REPARSE_TAG_MOUNT_POINT``: what ``mklink /J`` creates on Windows."""

#: Directory names that are a *system* directory when they sit directly on a
#: volume root.  Their contents are fair game (``C:\\Windows\\Temp`` is a T1
#: quarantine target); the directory itself is not.
WINDOWS_SYSTEM_DIRS = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "recovery",
        "perflogs",
        "$recycle.bin",
        "system volume information",
        "users",
    }
)

#: POSIX equivalents: a top-level directory nobody may quarantine (again, only
#: the directory itself -- ``/var/cache/pip`` is a normal cleanup target).
POSIX_SYSTEM_DIRS = frozenset(
    {
        "bin",
        "sbin",
        "lib",
        "lib32",
        "lib64",
        "libx32",
        "boot",
        "dev",
        "etc",
        "home",
        "media",
        "mnt",
        "opt",
        "proc",
        "root",
        "run",
        "srv",
        "sys",
        "usr",
        "var",
        "System",
        "Volumes",
        "Applications",
    }
)

#: Containers of user profiles: their direct children are profile roots.
PROFILE_PARENTS = frozenset({"users", "home"})


class ExecutorError(RuntimeError):
    """A run must refuse to continue (bad plan, bad manifest, unsafe request)."""


def _optional_text(value: object) -> str | None:
    """A JSON string field, or ``None`` (used when rebuilding journal digests)."""
    if value is None or isinstance(value, str):
        return value
    raise ExecutorError(f"expected a string, got {value!r}")


def _as_int(value: object) -> int:
    """A JSON number field, or ``0`` when the record left it out."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


@dataclass(frozen=True)
class PrimResult:
    """What one platform primitive did (or refused to do)."""

    ok: bool
    detail: str = ""
    """Plain-language outcome, shown in the report and stored in the journal."""

    command: tuple[str, ...] = ()
    """The external command that ran (``robocopy``, ``mklink``, ``compact``)."""

    note: str | None = None
    """A caveat worth surfacing (a fallback was used, a link was emulated)."""

    fallback: bool = False
    """True when the primary tool failed and a fallback carried the operation."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (journal and report)."""
        return {
            "ok": self.ok,
            "detail": self.detail,
            "command": list(self.command),
            "note": self.note,
            "fallback": self.fallback,
        }


class Hashing(Protocol):
    """The slice of ``hashlib`` the digest helpers use."""

    def update(self, data: bytes, /) -> None: ...


@dataclass(frozen=True)
class TreeDigest:
    """Identity of one payload (a file, a directory or a link) as it sits on disk.

    The digest is *location independent*: paths inside a directory payload are
    relative to it and a single file is recorded as ``.``, so the digest of a
    payload before a move and of the same payload at its destination compare
    equal.  ``tree_sha256`` covers every entry (path, kind, size, link target);
    ``content_sha256`` additionally covers the bytes when the payload is small
    enough and every file could be read.
    """

    path: str
    exists: bool
    is_dir: bool
    is_link: bool
    files: int
    dirs: int
    bytes: int
    tree_sha256: str | None
    content_sha256: str | None
    content_complete: bool
    unreadable: int = 0
    """Entries that could not be inspected (permission errors, races)."""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (journal and report)."""
        return {
            "path": self.path,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "is_link": self.is_link,
            "files": self.files,
            "dirs": self.dirs,
            "bytes": self.bytes,
            "tree_sha256": self.tree_sha256,
            "content_sha256": self.content_sha256,
            "content_complete": self.content_complete,
            "unreadable": self.unreadable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> TreeDigest:
        """Rebuild a digest from the journal (what undo verifies against)."""
        return cls(
            path=str(data.get("path") or ""),
            exists=bool(data.get("exists")),
            is_dir=bool(data.get("is_dir")),
            is_link=bool(data.get("is_link")),
            files=_as_int(data.get("files")),
            dirs=_as_int(data.get("dirs")),
            bytes=_as_int(data.get("bytes")),
            tree_sha256=_optional_text(data.get("tree_sha256")),
            content_sha256=_optional_text(data.get("content_sha256")),
            content_complete=bool(data.get("content_complete", True)),
            unreadable=_as_int(data.get("unreadable")),
        )


@runtime_checkable
class Backend(Protocol):
    """The platform primitives the dispatcher drives (one module per platform)."""

    name: str
    """``posix`` or ``win`` -- recorded in the journal and the report."""

    def path_for_os(self, path: str) -> str:
        """The path as the OS should see it (Windows long paths get a prefix)."""
        ...

    def can_link(self, kind: str, *, is_dir: bool) -> tuple[bool, str]:
        """Can this process create ``kind`` (``JUNCTION``/``SYMLINK``/``HARDLINK``)?

        Returns ``(ok, reason)``; ``reason`` explains a refusal in plain words
        (missing elevation, hard links on a directory, ...).
        """
        ...

    def can_compress(self) -> tuple[bool, str]:
        """Is in-place NTFS compression available here? ``(ok, reason)``."""
        ...

    def is_locked(self, path: str) -> tuple[bool, str]:
        """Best-effort "in use" probe; ``(locked, reason)``."""
        ...

    def move(self, src: str, dest: str) -> PrimResult:
        """Move ``src`` to ``dest`` (creating ``dest``'s parent as needed)."""
        ...

    def create_link(self, *, path: str, target: str, kind: str, is_dir: bool) -> PrimResult:
        """Create the link ``path`` -> ``target``."""
        ...

    def remove_link(self, path: str) -> PrimResult:
        """Remove a link at ``path`` (never its target's contents)."""
        ...

    def compress(self, path: str) -> PrimResult:
        """Compress ``path`` in place."""
        ...

    def uncompress(self, path: str) -> PrimResult:
        """Undo :meth:`compress`."""
        ...

    def default_quarantine_root(self, path: str) -> str:
        """Where a payload at ``path`` is quarantined (on its own volume)."""
        ...


# --------------------------------------------------------------------------- #
# Path arithmetic (works for the path style written, not for the host OS)
# --------------------------------------------------------------------------- #


def is_windows_path(path: str) -> bool:
    """True for ``C:\\...``-style and UNC paths (every WizTree export names those)."""
    drive, _ = ntpath.splitdrive(path)
    return bool(drive)


def path_components(path: str) -> tuple[str, ...]:
    """The components below the volume root (both separators accepted)."""
    if is_windows_path(path):
        _, rest = ntpath.splitdrive(path)
        return tuple(part for part in rest.replace("/", "\\").split("\\") if part)
    return tuple(part for part in path.split("/") if part)


def volume_root(path: str) -> str:
    """The volume a path lives on: ``C:\\``, ``\\\\server\\share\\`` or ``/``."""
    if is_windows_path(path):
        drive, _ = ntpath.splitdrive(path)
        return drive.replace("/", "\\") + "\\"
    return "/"


def volume_label(path: str) -> str:
    """A filesystem-safe name for that volume: ``C`` or ``server_share``."""
    if not is_windows_path(path):
        return "root"
    drive = ntpath.splitdrive(path)[0].replace("/", "\\")
    if len(drive) == 2 and drive.endswith(":"):
        return drive[0].upper()
    parts = [part for part in drive.replace("\\", "/").split("/") if part]
    return "_".join(parts).replace(":", "_") or "root"


def quarantine_relative(path: str) -> tuple[str, ...]:
    """Where a payload sits below its quarantine root.

    Windows paths keep their volume as the first component (``C:\\Users\\a`` ->
    ``C/Users/a``) so two drives never collide; POSIX paths are already
    absolute and unique.
    """
    components = path_components(path)
    if is_windows_path(path):
        return (volume_label(path), *components)
    return components


def plan_token(plan_id: str) -> str:
    """The filesystem-safe token a plan's quarantine (and journal) is keyed by."""
    match = PLAN_TOKEN_RE.match(plan_id)
    if match is None:
        raise ExecutorError(f"plan id {plan_id!r} must look like 'sha256:<64 hex>'")
    return match.group(1)[:PLAN_TOKEN_LENGTH]


def join_path(base: str, *parts: str) -> str:
    """Join in the style of ``base`` (so POSIX tests can drive Windows-shaped plans)."""
    if is_windows_path(base):
        result = base.rstrip("\\/")
        if result.endswith(":"):
            # A drive root must stay a root, or ntpath.join would turn
            # "D:\\" into the drive-relative "D:x".
            result += "\\"
        for part in parts:
            result = ntpath.join(result, part)
        return result
    result = base.rstrip("/")
    for part in parts:
        result = os.path.join(result or "/", part)
    return result or "/"


def is_under(path: str, root: str) -> bool:
    """Is ``path`` below (or equal to) ``root``? Case-insensitive for Windows paths."""
    if is_windows_path(path) or is_windows_path(root):
        subject = ntpath.normcase(ntpath.normpath(path)).replace("/", "\\")
        base = ntpath.normcase(ntpath.normpath(root)).replace("/", "\\").rstrip("\\")
        return subject == base or subject.startswith(base + "\\")
    subject = os.path.normpath(path)
    base = os.path.normpath(root).rstrip("/")
    return subject == base or subject.startswith(base + "/")


# --------------------------------------------------------------------------- #
# Guards: what must never be touched
# --------------------------------------------------------------------------- #


def protected_reason(path: str) -> str | None:
    """Why an operation on ``path`` itself is refused, or ``None`` when it is fine.

    Refused (design section 2): non-absolute paths, wildcards, volume roots,
    system directories, user profile roots, the home directory and any
    quarantine store.  The *contents* of these directories are not blocked --
    ``C:\\Windows\\Temp`` and ``/var/cache/pip`` are legitimate quarantine
    targets.
    """
    if not path:
        return "the path is empty"
    if any(char in path for char in "*?"):
        return "wildcards are not allowed (absolute paths only)"
    if is_windows_path(path):
        _, rest = ntpath.splitdrive(path)
        if rest and not rest.replace("/", "\\").startswith("\\"):
            return "the path is not absolute"
        components = path_components(path)
        if not components:
            return "the volume root"
        if len(components) == 1 and components[0].lower() in WINDOWS_SYSTEM_DIRS:
            return f"the system directory {components[0]}"
        if len(components) == 2 and components[0].lower() in PROFILE_PARENTS:
            return f"the profile root {path}"
        return _quarantine_reason(components)
    if not os.path.isabs(path):
        return "the path is not absolute"
    components = path_components(path)
    if not components:
        return "the volume root"
    if len(components) == 1 and components[0] in POSIX_SYSTEM_DIRS:
        return f"the system directory /{components[0]}"
    if len(components) == 2 and components[0] in PROFILE_PARENTS:
        return f"the profile root {path}"
    home = os.path.expanduser("~")
    if home != "~" and os.path.normpath(path) == os.path.normpath(home):
        return "the home directory"
    return _quarantine_reason(components)


def _quarantine_reason(components: tuple[str, ...]) -> str | None:
    """Refuse the quarantine store itself (it is not a cleanup target)."""
    if components and components[-1] in (QUARANTINE_DIRNAME, POSIX_QUARANTINE_DIRNAME):
        return "the quarantine store"
    return None


def reparse_kind(path: str) -> str | None:
    """``'symlink'`` / ``'junction'`` / ``'reparse'`` for a link, else ``None``.

    Junctions are not symlinks on Windows: ``os.path.islink`` reports ``False``
    for them, so the reparse tag (Python 3.12+ ``st_reparse_tag``, otherwise the
    ``FILE_ATTRIBUTE_REPARSE_POINT`` attribute) is what identifies them.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        if _is_junction(info):
            return "junction"
        return "symlink"
    if _has_reparse_attribute(info):
        tag = getattr(info, "st_reparse_tag", 0)
        if tag == REPARSE_TAG_MOUNT_POINT:
            return "junction"
        return "reparse"
    return None


def _is_junction(info: os.stat_result) -> bool:
    """A link whose reparse tag says it is a mount point (``mklink /J``)."""
    return bool(getattr(info, "st_reparse_tag", 0) == REPARSE_TAG_MOUNT_POINT)


def _has_reparse_attribute(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def is_reparse(path: str) -> bool:
    """True for symlinks, junctions and every other reparse point."""
    return reparse_kind(path) is not None


def is_compressed(path: str) -> bool:
    """True when Windows reports the entry as NTFS-compressed."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return _has_compressed_attribute(info)


def _has_compressed_attribute(info: os.stat_result) -> bool:
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_COMPRESSED", 0)))


def device(path: str) -> int | None:
    """``st_dev`` of ``path``, or of its nearest existing ancestor.

    Destination parents do not exist yet when an op is re-validated, so the walk
    up is what makes "is this a same-volume move?" answerable *before* anything
    is created.
    """
    probe = os.path.abspath(path)
    while True:
        try:
            return os.stat(probe).st_dev
        except OSError:
            parent = os.path.dirname(probe)
            if parent == probe:
                return None
            probe = parent


def same_volume(first: str, second: str) -> bool:
    """Do two paths live on the same volume (what a cheap move needs)?"""
    if is_windows_path(first) and is_windows_path(second):
        return volume_root(first).lower() == volume_root(second).lower()
    left = device(first)
    right = device(second)
    return left is not None and left == right


def link_points_at(path: str, target: str, kind: str) -> tuple[bool, str]:
    """Is ``path`` the link the executor just created? ``(ok, detail)``."""
    if kind == "HARDLINK":
        try:
            same = os.path.samefile(path, target)
        except OSError as exc:
            return False, f"cannot compare with the destination: {exc}"
        return same, ("the destination's payload" if same else "a different payload")
    if reparse_kind(path) is None:
        return False, "the link is not there"
    try:
        want = os.path.realpath(target)
        got = os.path.realpath(path)
    except OSError as exc:
        return False, f"cannot resolve the link: {exc}"
    if os.path.normcase(got) == os.path.normcase(want):
        return True, f"points at {got}"
    return False, f"points at {got}, expected {want}"


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #


def digest(path: str, *, content_limit: int = DEFAULT_CONTENT_LIMIT) -> TreeDigest:
    """Identify the payload at ``path`` (never following a link).

    A missing path is not an error: it yields ``exists=False`` and empty
    counters, which is exactly what the "did the source disappear" checks need.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return _missing(path)
    if reparse_kind(path) is not None:
        return _link_digest(path)
    if stat.S_ISDIR(info.st_mode):
        return _dir_digest(path, content_limit=content_limit)
    return _file_digest(path, size=info.st_size, content_limit=content_limit)


def _missing(path: str) -> TreeDigest:
    return TreeDigest(
        path=path,
        exists=False,
        is_dir=False,
        is_link=False,
        files=0,
        dirs=0,
        bytes=0,
        tree_sha256=None,
        content_sha256=None,
        content_complete=True,
    )


def _link_digest(path: str) -> TreeDigest:
    """A link payload: its identity is the target string, never the target's content."""
    try:
        target = os.readlink(path)
    except OSError:
        target = ""
    tree = hashlib.sha256(_line(".", "l", target)).hexdigest()
    return TreeDigest(
        path=path,
        exists=True,
        is_dir=False,
        is_link=True,
        files=0,
        dirs=0,
        bytes=0,
        tree_sha256=_HEX_PREFIX + tree,
        content_sha256=None,
        content_complete=True,
    )


def _file_digest(path: str, *, size: int, content_limit: int) -> TreeDigest:
    tree = hashlib.sha256(_line(".", "f", str(size))).hexdigest()
    content: str | None = None
    complete = False
    if size <= content_limit:
        hasher = hashlib.sha256()
        try:
            complete = _hash_file(hasher, path, size)
        except OSError:
            complete = False
        content = _HEX_PREFIX + hasher.hexdigest() if complete else None
    return TreeDigest(
        path=path,
        exists=True,
        is_dir=False,
        is_link=False,
        files=1,
        dirs=0,
        bytes=size,
        tree_sha256=_HEX_PREFIX + tree,
        content_sha256=content,
        content_complete=complete,
    )


def _dir_digest(root: str, *, content_limit: int) -> TreeDigest:
    entries, unreadable = _collect(root)
    tree = hashlib.sha256()
    files = 0
    dirs = 0
    total = 0
    for relpath, kind, payload in sorted(entries):
        tree.update(_line(relpath, kind, payload))
        if kind == "f":
            files += 1
            total += int(payload)
        elif kind == "d":
            dirs += 1
    content: str | None = None
    complete = False
    if total <= content_limit and unreadable == 0:
        hasher = hashlib.sha256()
        complete = True
        for relpath, kind, _payload in sorted(entries):
            if kind != "f":
                continue
            hasher.update(relpath.encode("utf-8", "surrogatepass") + b"\0")
            try:
                if not _hash_file(hasher, os.path.join(root, *relpath.split("/")), -1):
                    complete = False
                    break
            except OSError:
                complete = False
                break
        content = _HEX_PREFIX + hasher.hexdigest() if complete else None
    return TreeDigest(
        path=root,
        exists=True,
        is_dir=True,
        is_link=False,
        files=files,
        dirs=dirs,
        bytes=total,
        tree_sha256=_HEX_PREFIX + tree.hexdigest(),
        content_sha256=content,
        content_complete=complete,
        unreadable=unreadable,
    )


def _collect(root: str) -> tuple[list[tuple[str, str, str]], int]:
    """Every entry below ``root`` as ``(relpath, kind, payload)``; links never followed.

    ``kind`` is ``f`` (file, payload = size), ``d`` (directory, payload = ``""``)
    or ``l`` (link, payload = its target string).  The walk keeps its own stack,
    so tree depth is not limited by the interpreter.
    """
    entries: list[tuple[str, str, str]] = []
    unreadable = 0
    stack: list[tuple[str, str]] = [(root, "")]
    while stack:
        absolute, relpath = stack.pop()
        try:
            with os.scandir(absolute) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            unreadable += 1
            continue
        for entry in children:
            child = entry.name if not relpath else f"{relpath}/{entry.name}"
            kind = reparse_kind(entry.path)
            if kind is not None:
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    target = ""
                entries.append((child, "l", target))
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                unreadable += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                entries.append((child, "d", ""))
                stack.append((entry.path, child))
            else:
                entries.append((child, "f", str(info.st_size)))
    return entries, unreadable


def _line(relpath: str, kind: str, payload: str) -> bytes:
    return f"{relpath}\0{kind}\0{payload}\n".encode("utf-8", "surrogatepass")


_HEX_PREFIX = "sha256:"


def _hash_file(hasher: Hashing, path: str, expected: int) -> bool:
    """Stream ``path`` into ``hasher``; ``False`` when the size moved under us.

    ``expected < 0`` means "whatever the file reports" (the directory pass
    compares nothing; the caller re-checks ``complete``).
    """
    read = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            hasher.update(chunk)
            read += len(chunk)
    return expected < 0 or read == expected


def verify(before: TreeDigest, after: TreeDigest) -> str:
    """``verified`` / ``mismatch`` / ``unavailable`` for a before/after pair."""
    if not (before.exists and after.exists):
        return "mismatch"
    if (before.files, before.dirs, before.bytes, before.is_dir) != (
        after.files,
        after.dirs,
        after.bytes,
        after.is_dir,
    ):
        return "mismatch"
    if before.content_sha256 is not None and after.content_sha256 is not None:
        return "verified" if before.content_sha256 == after.content_sha256 else "mismatch"
    if before.tree_sha256 is None or after.tree_sha256 is None:
        return "unavailable"
    return "verified" if before.tree_sha256 == after.tree_sha256 else "mismatch"
