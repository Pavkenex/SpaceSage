"""Optional live-filesystem scan for exact duplicate files.

The candidate stage flags same-name/same-size clusters as ``dupes-weak``: those
bytes are **not** verified, so the planner can only keep them as review items
(``docs/design.md`` sections 6.2 and 7.1).  This module is the other half -- an
optional scan that runs **where the files are** (not on the export) and proves
which copies are byte-identical.  Pure arithmetic, no AI.

What one call does:

1. Walk every root with :func:`os.scandir`, **never following symlinks,
   junctions or any other reparse point**: a link is counted and skipped, never
   descended into, so the scan can neither loop nor read outside the tree the
   user named.  The roots themselves must not be links either.
2. Keep regular files at or above ``min_size`` (default 1 MiB) and group them
   by size -- a size seen once cannot have a duplicate, so it is never read.
3. Hash the first :data:`PARTIAL_BYTES` (64 KiB) of every remaining candidate
   and group by that prefix; only groups that still hold two members get their
   full content read.  A group's ``sha256`` always covers the whole file, so a
   shared 64 KiB prefix is never enough to call two files identical.  Files no
   larger than the partial window are fully covered by the first read and are
   not read twice.
4. Split each verified group into **copies** -- one per distinct file identity
   (device + inode) -- because hard-linked paths share one payload.  The
   recoverable figure is therefore ``(copies - 1) x size`` and never
   ``(paths - 1) x size``.  A verified group whose paths are all *one* physical
   copy is reported as a hardlink set instead, with the bytes already saved, so
   nobody "cleans up" hardlinks expecting space back.

Every group carries a suggested keep policy -- keep the **newest** copy, break
ties with the **shortest path**, then lexicographically, so the answer never
depends on directory order -- and a hardlink-dedupe suggestion: replacing every
other copy with a hardlink to the kept one reclaims the same bytes while
*every* path stays valid.  That is only feasible when all copies live on one
volume (hardlinks cannot cross volumes) and the filesystem reports file
identities; otherwise the suggestion says why, and deleting or moving the extra
copies is the only way to reclaim.

The scan is **strictly read-only**: files are opened for reading only, nothing
is written, moved, linked or deleted, and nothing is "repaired".  A file that
changes between the walk and the read is dropped and counted (``changed``)
rather than reported as a duplicate of a snapshot that no longer exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from spacesage.stats import format_bytes

SCHEMA = "spacesage.deepscan/v1"
"""Schema string of :meth:`ScanReport.to_dict`."""

DEFAULT_MIN_SIZE = 1024**2
"""Files smaller than this are never hashed (1 MiB, like the dupe floor)."""

PARTIAL_BYTES = 64 * 1024
"""Bytes read by the first (partial) hash pass -- 64 KiB."""

DEFAULT_TOP = 20
"""Groups listed by :func:`render_text` unless ``top`` says otherwise."""

KEEP_POLICY = "newest-then-shortest-path"
"""Default keep policy: newest copy, ties broken by the shortest path."""

ROLE_KEEP = "keep"
ROLE_DUPLICATE = "duplicate"
ROLE_HARDLINK = "hardlink"
"""Member roles: the suggested survivor, a physical copy, an extra hard link."""

ROLES: tuple[str, ...] = (ROLE_KEEP, ROLE_DUPLICATE, ROLE_HARDLINK)

MAX_ERROR_SAMPLES = 20
"""Issues kept in the report; the counters always cover every one of them."""

_READ_CHUNK = 1024 * 1024
_PROGRESS_FILES = 2000
_PROGRESS_HASHES = 200
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_DAY = 86_400


class DeepScanError(RuntimeError):
    """Raised when a scan cannot start for the given roots or options."""


# --------------------------------------------------------------------------- #
# Report data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScanIssue:
    """One path the scan could not read, with the reason in plain language."""

    path: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class ScanProgress:
    """Progress snapshot handed to the optional callback.

    ``files``/``bytes`` count the regular files walked so far, ``candidates``
    those at or above ``min_size``, and ``hashed``/``bytes_read`` the hash
    reads actually performed (partial and full together).
    """

    stage: str
    """``walk`` | ``partial`` | ``full`` | ``done``."""

    files: int
    bytes: int
    candidates: int
    hashed: int
    bytes_read: int
    elapsed_s: float
    path: str

    @property
    def files_per_sec(self) -> float:
        """Regular files walked per second so far."""
        return self.files / self.elapsed_s if self.elapsed_s > 0 else 0.0


@dataclass(frozen=True)
class ScanStats:
    """What the walk and the hash passes saw, in counts."""

    files: int
    """Regular files walked (a link, socket or device is not one)."""

    bytes: int
    """Total size of those files."""

    candidates: int
    """Files at or above ``min_size`` that entered the size grouping."""

    candidate_bytes: int
    skipped_small: int
    skipped_links: int
    skipped_special: int
    unreadable: int
    """Files dropped because they could not be read."""

    changed: int
    """Files dropped because they changed between the walk and the read."""

    reused_reads: int
    """Paths hashed by file identity instead of reading one file twice."""

    partial_reads: int
    """First-64 KiB reads performed."""

    full_reads: int
    """Whole-file reads performed (files larger than the partial window)."""

    bytes_read: int
    """Bytes actually read from disk across both passes."""

    errors: int
    error_samples: tuple[ScanIssue, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "candidates": self.candidates,
            "candidate_bytes": self.candidate_bytes,
            "skipped_small": self.skipped_small,
            "skipped_links": self.skipped_links,
            "skipped_special": self.skipped_special,
            "unreadable": self.unreadable,
            "changed": self.changed,
            "reused_reads": self.reused_reads,
            "partial_reads": self.partial_reads,
            "full_reads": self.full_reads,
            "bytes_read": self.bytes_read,
            "errors": self.errors,
            "error_samples": [issue.to_dict() for issue in self.error_samples],
        }


@dataclass(frozen=True)
class GroupMember:
    """One path inside a verified group."""

    path: str
    size: int
    mtime: int | None
    """Epoch seconds of the last modification (``None`` when unknown)."""

    dev: int | None
    """Volume identity (``st_dev``); ``None`` when the filesystem has none."""

    ino: int | None
    """File identity (``st_ino``); ``0``/``None`` means the filesystem does
    not report one."""

    links: int
    """Hard link count at scan time (``st_nlink``)."""

    role: str
    """One of :data:`ROLES`."""

    same_as: str | None = None
    """For a ``hardlink`` member: the listed path it shares its file with."""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "mtime": _iso_optional(self.mtime),
            "dev": self.dev,
            "ino": self.ino,
            "links": self.links,
            "role": self.role,
            "same_as": self.same_as,
        }


@dataclass(frozen=True)
class HardlinkPlan:
    """Same-volume hardlink-dedupe suggestion for one duplicate group."""

    feasible: bool
    link_to: str | None
    """The kept path every other copy would be linked to."""

    relink: tuple[str, ...]
    """Paths the dedupe would re-create as hardlinks to ``link_to``."""

    reclaimable_bytes: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "feasible": self.feasible,
            "link_to": self.link_to,
            "relink": list(self.relink),
            "reclaimable_bytes": self.reclaimable_bytes,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DuplicateGroup:
    """Two or more byte-identical copies of one file, hash-verified."""

    group_id: str
    """``g1``, ``g2``, ... positionally over the report's order."""

    size: int
    """Size of one copy (every member has exactly this size)."""

    copies: int
    """Distinct physical copies (one per device + file identity)."""

    paths: int
    """Paths in the group (``paths - copies`` of them are hard links)."""

    reclaimable_bytes: int
    """``(copies - 1) x size`` -- what deleting every extra copy frees."""

    linked_bytes: int
    """Bytes already saved by hard links inside the group."""

    sha256: str
    """Digest of the whole file -- the group's identity proof."""

    partial_sha256: str
    """Digest of the first :data:`PARTIAL_BYTES` (equal to ``sha256`` when the
    file is no larger than the partial window)."""

    keep: str
    keep_policy: str
    keep_reason: str
    members: tuple[GroupMember, ...]
    hardlink: HardlinkPlan

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.group_id,
            "size": self.size,
            "copies": self.copies,
            "paths": self.paths,
            "reclaimable_bytes": self.reclaimable_bytes,
            "linked_bytes": self.linked_bytes,
            "sha256": self.sha256,
            "partial_sha256": self.partial_sha256,
            "keep": self.keep,
            "keep_policy": self.keep_policy,
            "keep_reason": self.keep_reason,
            "members": [member.to_dict() for member in self.members],
            "hardlink": self.hardlink.to_dict(),
        }


@dataclass(frozen=True)
class HardlinkSet:
    """Verified identical paths that are *one* physical file (nothing to free).

    Reported so a user does not "deduplicate" hard links expecting space back:
    the payload is already counted once, and only removing the last path frees
    anything.
    """

    set_id: str
    """``h1``, ``h2``, ... positionally over the report's order."""

    size: int
    paths: int
    linked_bytes: int
    """``(paths - 1) x size``: what these paths would cost if not linked."""

    sha256: str
    partial_sha256: str
    keep: str
    keep_policy: str
    keep_reason: str
    members: tuple[GroupMember, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.set_id,
            "size": self.size,
            "paths": self.paths,
            "linked_bytes": self.linked_bytes,
            "sha256": self.sha256,
            "partial_sha256": self.partial_sha256,
            "keep": self.keep,
            "keep_policy": self.keep_policy,
            "keep_reason": self.keep_reason,
            "members": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class ScanSummary:
    """Totals across the report's groups and hardlink sets."""

    groups: int
    paths: int
    copies: int
    reclaimable_bytes: int
    hardlink_sets: int
    hardlink_paths: int
    linked_bytes: int
    same_volume_groups: int
    mixed_volume_groups: int

    def to_dict(self) -> dict[str, object]:
        return {
            "groups": self.groups,
            "paths": self.paths,
            "copies": self.copies,
            "reclaimable_bytes": self.reclaimable_bytes,
            "hardlink_sets": self.hardlink_sets,
            "hardlink_paths": self.hardlink_paths,
            "linked_bytes": self.linked_bytes,
            "same_volume_groups": self.same_volume_groups,
            "mixed_volume_groups": self.mixed_volume_groups,
        }


@dataclass(frozen=True)
class ScanReport:
    """Everything one scan found, in one object."""

    roots: tuple[str, ...]
    notes: tuple[str, ...]
    min_size: int
    partial_bytes: int
    as_of: int
    elapsed_s: float
    stats: ScanStats
    groups: tuple[DuplicateGroup, ...]
    hardlink_sets: tuple[HardlinkSet, ...]

    @property
    def summary(self) -> ScanSummary:
        """Totals over both lists."""
        return ScanSummary(
            groups=len(self.groups),
            paths=sum(group.paths for group in self.groups),
            copies=sum(group.copies for group in self.groups),
            reclaimable_bytes=sum(group.reclaimable_bytes for group in self.groups),
            hardlink_sets=len(self.hardlink_sets),
            hardlink_paths=sum(item.paths for item in self.hardlink_sets),
            linked_bytes=sum(group.linked_bytes for group in self.groups)
            + sum(item.linked_bytes for item in self.hardlink_sets),
            same_volume_groups=sum(1 for group in self.groups if group.hardlink.feasible),
            mixed_volume_groups=sum(1 for group in self.groups if not group.hardlink.feasible),
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (``spacesage.deepscan/v1``)."""
        return {
            "schema": SCHEMA,
            "roots": list(self.roots),
            "notes": list(self.notes),
            "as_of": _iso(self.as_of),
            "elapsed_s": round(self.elapsed_s, 3),
            "thresholds": {
                "min_size": self.min_size,
                "partial_bytes": self.partial_bytes,
                "keep_policy": KEEP_POLICY,
            },
            "stats": self.stats.to_dict(),
            "summary": self.summary.to_dict(),
            "groups": [group.to_dict() for group in self.groups],
            "hardlink_sets": [item.to_dict() for item in self.hardlink_sets],
        }


# --------------------------------------------------------------------------- #
# Link and path helpers
# --------------------------------------------------------------------------- #


def _reparse_reason(entry_stat: os.stat_result, entry: object = None) -> str | None:
    """The link type of an entry, or ``None`` when it is a plain file/dir.

    Catches what the platform offers: POSIX symlinks, Windows symlinks, and
    junctions or any other reparse point (a directory junction is *not* a
    symlink to :func:`os.lstat`).  Returning a reason means "do not follow".
    """
    if entry is not None:
        is_symlink = getattr(entry, "is_symlink", None)
        if callable(is_symlink):
            try:
                if bool(is_symlink()):
                    return "symlink"
            except OSError:
                return "unreadable entry"
    if stat.S_ISLNK(entry_stat.st_mode):
        return "symlink"
    if getattr(entry_stat, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE:
        return "reparse point"
    if getattr(entry_stat, "st_reparse_tag", 0):
        return "reparse point"
    if entry is not None:
        is_junction = getattr(entry, "is_junction", None)
        if callable(is_junction):
            try:
                if bool(is_junction()):
                    return "junction"
            except OSError:
                return "unreadable entry"
    return None


def _root_link_reason(path: str) -> str | None:
    """The link type of a path itself (no directory entry available)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE:
        return "reparse point"
    if getattr(info, "st_reparse_tag", 0):
        return "reparse point"
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction):
        try:
            if bool(is_junction(path)):
                return "junction"
        except OSError:
            return "reparse point"
    return None


def _path_components(path: str) -> int:
    """Number of components in a path (either separator style)."""
    return sum(1 for part in path.replace("\\", "/").split("/") if part)


def _is_under(path: str, parent: str) -> bool:
    """True when ``path`` is ``parent`` or lives below it (case-aware)."""
    child_key = os.path.normcase(path).rstrip("\\/")
    parent_key = os.path.normcase(parent).rstrip("\\/")
    if not parent_key:
        return True
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key + os.sep) or child_key.startswith(parent_key + "/")


def _normalize_roots(roots: Sequence[str | os.PathLike[str]], notes: list[str]) -> tuple[str, ...]:
    """Absolute, de-duplicated, non-nested roots; hard error on a bad one."""
    if not roots:
        raise DeepScanError("at least one root is required")
    cleaned: list[str] = []
    for raw in roots:
        path = os.path.abspath(os.fspath(raw))
        if path != os.sep and len(path) > 1:
            path = path.rstrip(os.sep) or path
        if not os.path.exists(path):
            raise DeepScanError(f"root does not exist: {path}")
        if not os.path.isdir(path):
            raise DeepScanError(f"root is not a directory: {path}")
        reason = _root_link_reason(path)
        if reason is not None:
            raise DeepScanError(
                f"root is a {reason} and the scan never follows links; "
                f"point it at the real path instead: {path}"
            )
        cleaned.append(path)
    kept: list[str] = []
    for path in sorted(set(cleaned)):
        covered = next((root for root in kept if _is_under(path, root)), None)
        if covered is not None:
            notes.append(f"root {path} is inside {covered}; scanned once")
            continue
        kept.append(path)
    return tuple(kept)


def _identity(dev: int | None, ino: int | None) -> tuple[object, ...] | None:
    """The file identity of a stat, or ``None`` when the filesystem has none."""
    if dev is None or not ino:
        return None
    return ("inode", dev, ino)


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #


def _hash_file(path: str, limit: int | None) -> tuple[str, int]:
    """sha256 of the whole file, or of its first ``limit`` bytes; read-only."""
    digest = hashlib.sha256()
    read = 0
    with open(path, "rb") as handle:  # read-only: the scan never writes
        while True:
            if limit is not None:
                want = min(_READ_CHUNK, limit - read)
                if want <= 0:
                    break
            else:
                want = _READ_CHUNK
            chunk = handle.read(want)
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
    return digest.hexdigest(), read


@dataclass(frozen=True)
class _ScanFile:
    """One walked file: what is needed to hash it and to report it."""

    path: str
    size: int
    mtime: int | None
    mtime_ns: int
    dev: int | None
    ino: int | None
    links: int

    @property
    def has_identity(self) -> bool:
        return _identity(self.dev, self.ino) is not None


class _Scanner:
    """State of one scan: the walk, the hash passes and the counters."""

    def __init__(
        self,
        roots: tuple[str, ...],
        notes: list[str],
        *,
        min_size: int,
        partial_bytes: int,
        progress: Callable[[ScanProgress], None] | None,
        started: float,
    ) -> None:
        self.roots = roots
        self.notes = notes
        self.min_size = min_size
        self.partial_bytes = partial_bytes
        self.progress = progress
        self.started = started
        self.files = 0
        self.byte_count = 0
        self.candidates = 0
        self.candidate_bytes = 0
        self.skipped_small = 0
        self.skipped_links = 0
        self.skipped_special = 0
        self.unreadable = 0
        self.changed = 0
        self.partial_reads = 0
        self.full_reads = 0
        self.bytes_read = 0
        self.error_count = 0
        self.error_samples: list[ScanIssue] = []
        self.reused_reads = 0
        self.by_size: dict[int, list[_ScanFile]] = {}
        self._digests: dict[tuple[tuple[object, ...], int | None], str] = {}

    # -- diagnostics -------------------------------------------------------- #

    def _issue(self, path: str, reason: str) -> None:
        self.error_count += 1
        if len(self.error_samples) < MAX_ERROR_SAMPLES:
            self.error_samples.append(ScanIssue(path=path, reason=reason))

    def _emit(self, stage: str, path: str) -> None:
        if self.progress is None:
            return
        self.progress(
            ScanProgress(
                stage=stage,
                files=self.files,
                bytes=self.byte_count,
                candidates=self.candidates,
                hashed=self.partial_reads + self.full_reads,
                bytes_read=self.bytes_read,
                elapsed_s=time.monotonic() - self.started,
                path=path,
            )
        )

    # -- walk --------------------------------------------------------------- #

    def walk(self) -> None:
        """Depth-first walk of every root; links are counted, never entered."""
        for root in self.roots:
            stack = [root]
            while stack:
                directory = stack.pop()
                try:
                    scan = os.scandir(directory)
                except OSError as exc:
                    self._issue(directory, f"cannot list directory: {_message(exc)}")
                    continue
                with scan:
                    for entry in scan:
                        self._visit(entry, stack)

    def _visit(self, entry: os.DirEntry[str], stack: list[str]) -> None:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            self._issue(entry.path, f"cannot stat: {_message(exc)}")
            return
        reason = _reparse_reason(info, entry)
        if reason is not None:
            self.skipped_links += 1
            return
        if stat.S_ISDIR(info.st_mode):
            stack.append(entry.path)
            return
        if not stat.S_ISREG(info.st_mode):
            self.skipped_special += 1
            return
        self.files += 1
        self.byte_count += info.st_size
        if info.st_size < self.min_size:
            self.skipped_small += 1
        else:
            self.candidates += 1
            self.candidate_bytes += info.st_size
            self.by_size.setdefault(info.st_size, []).append(
                _ScanFile(
                    path=entry.path,
                    size=info.st_size,
                    mtime=int(info.st_mtime),
                    mtime_ns=info.st_mtime_ns,
                    dev=_int_or_none(getattr(info, "st_dev", None)),
                    ino=_int_or_none(getattr(info, "st_ino", None)),
                    links=int(getattr(info, "st_nlink", 1) or 1),
                )
            )
        if self.files % _PROGRESS_FILES == 0:
            self._emit("walk", entry.path)

    # -- hashing ------------------------------------------------------------ #

    def _digest(self, file: _ScanFile, *, limit: int | None, stage: str) -> str | None:
        """Read one file and return its digest, or ``None`` when it is unusable.

        A path that shares its file identity with a path already hashed is not
        read again -- the same file cannot have a different digest, and reading
        a hard-linked 4 GB blob twice helps nobody.
        """
        key = _identity(file.dev, file.ino)
        cache_key = (key, limit) if key is not None else None
        if cache_key is not None:
            cached = self._digests.get(cache_key)
            if cached is not None:
                self.reused_reads += 1
                return cached
        expected = min(limit, file.size) if limit is not None else file.size
        try:
            before = os.stat(file.path)
            if (before.st_size, before.st_mtime_ns) != (file.size, file.mtime_ns):
                self._stale(file)
                return None
            digest, read = _hash_file(file.path, limit)
            after = os.stat(file.path)
        except OSError as exc:
            self.unreadable += 1
            self._issue(file.path, f"cannot read: {_message(exc)}")
            return None
        if read != expected or (after.st_size, after.st_mtime_ns) != (file.size, file.mtime_ns):
            self._stale(file)
            return None
        self.bytes_read += read
        if stage == "partial":
            self.partial_reads += 1
        else:
            self.full_reads += 1
        if cache_key is not None:
            self._digests[cache_key] = digest
        if (self.partial_reads + self.full_reads) % _PROGRESS_HASHES == 0:
            self._emit(stage, file.path)
        return digest

    def _stale(self, file: _ScanFile) -> None:
        self.changed += 1
        self._issue(file.path, "changed while scanning; skipped")

    def hash_groups(self) -> list[tuple[int, str, str, list[_ScanFile]]]:
        """Verified content groups as ``(size, sha256, partial, files)``."""
        partials: dict[tuple[int, str], list[_ScanFile]] = {}
        for size in sorted(self.by_size):
            files = self.by_size[size]
            if len(files) < 2:
                continue
            for file in sorted(files, key=lambda item: item.path):
                digest = self._digest(file, limit=self.partial_bytes, stage="partial")
                if digest is None:
                    continue
                partials.setdefault((size, digest), []).append(file)
        verified: list[tuple[int, str, str, list[_ScanFile]]] = []
        for (size, prefix), files in sorted(partials.items()):
            if len(files) < 2:
                continue
            if size <= self.partial_bytes:
                # The partial read covered the whole file: no second pass.
                verified.append((size, prefix, prefix, files))
                continue
            buckets: dict[str, list[_ScanFile]] = {}
            for file in files:
                digest = self._digest(file, limit=None, stage="full")
                if digest is None:
                    continue
                buckets.setdefault(digest, []).append(file)
            for digest, bucket in sorted(buckets.items()):
                if len(bucket) >= 2:
                    verified.append((size, digest, prefix, bucket))
        return verified

    # -- report ------------------------------------------------------------- #

    def stats(self) -> ScanStats:
        return ScanStats(
            files=self.files,
            bytes=self.byte_count,
            candidates=self.candidates,
            candidate_bytes=self.candidate_bytes,
            skipped_small=self.skipped_small,
            skipped_links=self.skipped_links,
            skipped_special=self.skipped_special,
            unreadable=self.unreadable,
            changed=self.changed,
            reused_reads=self.reused_reads,
            partial_reads=self.partial_reads,
            full_reads=self.full_reads,
            bytes_read=self.bytes_read,
            errors=self.error_count,
            error_samples=tuple(self.error_samples),
        )


# --------------------------------------------------------------------------- #
# Group building
# --------------------------------------------------------------------------- #


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _message(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _stamp(mtime: int) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%d %H:%M") + " UTC"


def _duration(seconds: int) -> str:
    if seconds >= 2 * _DAY:
        return f"{seconds // _DAY} d"
    if seconds >= 7200:
        return f"{seconds // 3600} h"
    if seconds >= 120:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _keep_key(file: _ScanFile) -> tuple[int, int, int, int, str]:
    """Newest first, then the shortest path, then lexicographic."""
    return (
        0 if file.mtime is not None else 1,
        -(file.mtime or 0),
        _path_components(file.path),
        len(file.path),
        file.path,
    )


def _keep_reason(files: Sequence[_ScanFile], keep: _ScanFile) -> str:
    """Plain-language reason for the suggested survivor."""
    total = len(files)
    stamped = [file for file in files if file.mtime is not None]
    if keep.mtime is None:
        return f"shortest of {total} paths (none carries a usable timestamp)"
    newest = [file for file in files if file.mtime == keep.mtime]
    if len(newest) == total:
        return f"shortest of {total} paths (all modified {_stamp(keep.mtime)})"
    if len(newest) == 1:
        if len(stamped) == total:
            oldest = min(file.mtime for file in stamped if file.mtime is not None)
            return (
                f"newest of {total} paths (modified {_stamp(keep.mtime)}, "
                f"{_duration(keep.mtime - oldest)} newer than the oldest)"
            )
        return (
            f"newest of {total} paths (modified {_stamp(keep.mtime)}); "
            f"{total - len(stamped)} of them have no timestamp"
        )
    return f"shortest of the {len(newest)} paths modified {_stamp(keep.mtime)}"


def _clusters(files: Sequence[_ScanFile]) -> list[list[_ScanFile]]:
    """One inner list per physical copy, ordered by their first path."""
    grouped: dict[tuple[object, ...] | None, list[_ScanFile]] = {}
    for file in sorted(files, key=lambda item: item.path):
        key = _identity(file.dev, file.ino)
        if key is None:
            key = ("path", file.path)  # no identity to share: its own copy
        grouped.setdefault(key, []).append(file)
    return [
        grouped[key]
        for key in sorted(grouped, key=lambda key: min(file.path for file in grouped[key]))
    ]


def _member(file: _ScanFile, role: str, *, same_as: str | None = None) -> GroupMember:
    return GroupMember(
        path=file.path,
        size=file.size,
        mtime=file.mtime,
        dev=file.dev,
        ino=file.ino,
        links=file.links,
        role=role,
        same_as=same_as,
    )


def _cluster_key(cluster: Sequence[_ScanFile]) -> tuple[object, ...]:
    """The identity every path in one cluster shares."""
    first = cluster[0]
    return _identity(first.dev, first.ino) or ("path", first.path)


def _layout(files: Sequence[_ScanFile], keep: _ScanFile) -> tuple[tuple[GroupMember, ...], int]:
    """Members of a verified group, the kept path first, and the copy count."""
    clusters = _clusters(files)
    keep_key = _identity(keep.dev, keep.ino) or ("path", keep.path)
    ordered = sorted(clusters, key=lambda cluster: _cluster_key(cluster) != keep_key)
    members: list[GroupMember] = []
    for cluster in ordered:
        paths = sorted(file.path for file in cluster)
        is_keep_cluster = keep.path in paths
        if is_keep_cluster:
            paths = [keep.path] + [path for path in paths if path != keep.path]
        first = paths[0]
        for path in paths:
            if is_keep_cluster and path == keep.path:
                members.append(_member(_lookup(cluster, path), ROLE_KEEP))
            elif path == first:
                members.append(_member(_lookup(cluster, path), ROLE_DUPLICATE))
            else:
                members.append(_member(_lookup(cluster, path), ROLE_HARDLINK, same_as=first))
    return tuple(members), len(clusters)


def _lookup(cluster: Sequence[_ScanFile], path: str) -> _ScanFile:
    for file in cluster:
        if file.path == path:
            return file
    raise AssertionError(f"{path!r} is not in its own cluster")  # pragma: no cover


def _member_identity(member: GroupMember) -> tuple[object, ...]:
    return _identity(member.dev, member.ino) or ("path", member.path)


def hardlink_dedupe(size: int, members: Sequence[GroupMember], keep: str) -> HardlinkPlan:
    """Suggest replacing every other copy with a hardlink to the kept one.

    Feasible only when every member sits on one known volume and the
    filesystem reports file identities -- hardlinks cannot cross volumes, and
    without identities there is nothing to link *to*.
    """
    if not members:
        return HardlinkPlan(
            feasible=False,
            link_to=None,
            relink=(),
            reclaimable_bytes=0,
            reason="the group has no members",
        )
    keep_member = next((member for member in members if member.path == keep), None)
    keep_key = _member_identity(keep_member) if keep_member is not None else None
    # Physical copies, keep's own file included: the other paths of the kept
    # file are *not* extra copies, they ride on the same payload.
    clusters: dict[tuple[object, ...], list[str]] = {}
    for member in members:
        clusters.setdefault(_member_identity(member), []).append(member.path)
    relink = tuple(path for key, paths in clusters.items() if key != keep_key for path in paths)
    reclaimable = len(clusters) - (1 if keep_key is not None else 0)
    reclaimable = max(reclaimable, 0) * size
    if not relink:
        return HardlinkPlan(
            feasible=False,
            link_to=keep,
            relink=(),
            reclaimable_bytes=0,
            reason="no second copy to link: every path is the same physical file",
        )
    volumes = {member.dev for member in members}
    if any(member.dev is None for member in members):
        return HardlinkPlan(
            feasible=False,
            link_to=keep,
            relink=relink,
            reclaimable_bytes=reclaimable,
            reason=(
                "the filesystem does not report a volume for every copy, so a "
                "same-volume hardlink cannot be confirmed; delete or move the extra copies"
            ),
        )
    if len(volumes) > 1:
        return HardlinkPlan(
            feasible=False,
            link_to=keep,
            relink=relink,
            reclaimable_bytes=reclaimable,
            reason=(
                f"the copies span {len(volumes)} volumes -- hardlinks cannot cross "
                f"volumes; delete or move the extra copies instead"
            ),
        )
    if any(not member.ino for member in members):
        return HardlinkPlan(
            feasible=False,
            link_to=keep,
            relink=relink,
            reclaimable_bytes=reclaimable,
            reason=(
                "the filesystem does not report file identities, so hardlink "
                "support cannot be confirmed; delete or move the extra copies"
            ),
        )
    return HardlinkPlan(
        feasible=True,
        link_to=keep,
        relink=relink,
        reclaimable_bytes=reclaimable,
        reason=(
            f"all {len(members)} paths are on one volume ({len(clusters)} extra "
            f"copies): replacing them with hardlinks to the kept file frees "
            f"{format_bytes(reclaimable)} and keeps every path valid"
        ),
    )


def _group_from(
    size: int,
    sha256: str,
    partial: str,
    files: Sequence[_ScanFile],
) -> DuplicateGroup | HardlinkSet:
    """Build the report item for one verified content group."""
    keep_file = min(files, key=_keep_key)
    members, copies = _layout(files, keep_file)
    keep_reason = _keep_reason(files, keep_file)
    if copies < 2:
        return HardlinkSet(
            set_id="",
            size=size,
            paths=len(members),
            linked_bytes=(len(members) - 1) * size,
            sha256=sha256,
            partial_sha256=partial,
            keep=keep_file.path,
            keep_policy=KEEP_POLICY,
            keep_reason=keep_reason,
            members=members,
        )
    return DuplicateGroup(
        group_id="",
        size=size,
        copies=copies,
        paths=len(members),
        reclaimable_bytes=(copies - 1) * size,
        linked_bytes=(len(members) - copies) * size,
        sha256=sha256,
        partial_sha256=partial,
        keep=keep_file.path,
        keep_policy=KEEP_POLICY,
        keep_reason=keep_reason,
        members=members,
        hardlink=hardlink_dedupe(size, members, keep_file.path),
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def scan(
    roots: Sequence[str | os.PathLike[str]] | str | os.PathLike[str],
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    partial_bytes: int = PARTIAL_BYTES,
    progress: Callable[[ScanProgress], None] | None = None,
    now: float | None = None,
) -> ScanReport:
    """Scan ``roots`` for byte-identical files.

    Read-only, deterministic for a static tree, and safe by construction: no
    symlink, junction or other reparse point is ever followed, no file is
    opened for writing, and a file that changes mid-scan is dropped and
    reported instead of being treated as a duplicate.
    """
    if min_size < 0:
        raise DeepScanError(f"min_size must not be negative, got {min_size}")
    if partial_bytes <= 0:
        raise DeepScanError(f"partial_bytes must be positive, got {partial_bytes}")
    selected = (roots,) if isinstance(roots, (str, os.PathLike)) else tuple(roots)
    notes: list[str] = []
    normalized = _normalize_roots(selected, notes)
    started = time.monotonic()
    scanner = _Scanner(
        normalized,
        notes,
        min_size=min_size,
        partial_bytes=partial_bytes,
        progress=progress,
        started=started,
    )
    scanner.walk()
    verified = scanner.hash_groups()
    groups: list[DuplicateGroup] = []
    hardlink_sets: list[HardlinkSet] = []
    for size, sha256, partial, files in verified:
        item = _group_from(size, sha256, partial, files)
        if isinstance(item, DuplicateGroup):
            groups.append(item)
        else:
            hardlink_sets.append(item)
    groups.sort(key=lambda group: (-group.reclaimable_bytes, -group.size, group.keep))
    hardlink_sets.sort(key=lambda item: (-item.linked_bytes, -item.size, item.keep))
    numbered_groups = tuple(
        _renumber_group(group, index) for index, group in enumerate(groups, start=1)
    )
    numbered_sets = tuple(
        _renumber_set(item, index) for index, item in enumerate(hardlink_sets, start=1)
    )
    report = ScanReport(
        roots=normalized,
        notes=tuple(notes),
        min_size=min_size,
        partial_bytes=partial_bytes,
        as_of=int(now if now is not None else time.time()),
        elapsed_s=time.monotonic() - started,
        stats=scanner.stats(),
        groups=numbered_groups,
        hardlink_sets=numbered_sets,
    )
    scanner._emit("done", normalized[-1] if normalized else "")
    return report


def _renumber_group(group: DuplicateGroup, index: int) -> DuplicateGroup:
    if group.group_id == f"g{index}":
        return group
    return DuplicateGroup(
        group_id=f"g{index}",
        size=group.size,
        copies=group.copies,
        paths=group.paths,
        reclaimable_bytes=group.reclaimable_bytes,
        linked_bytes=group.linked_bytes,
        sha256=group.sha256,
        partial_sha256=group.partial_sha256,
        keep=group.keep,
        keep_policy=group.keep_policy,
        keep_reason=group.keep_reason,
        members=group.members,
        hardlink=group.hardlink,
    )


def _renumber_set(item: HardlinkSet, index: int) -> HardlinkSet:
    if item.set_id == f"h{index}":
        return item
    return HardlinkSet(
        set_id=f"h{index}",
        size=item.size,
        paths=item.paths,
        linked_bytes=item.linked_bytes,
        sha256=item.sha256,
        partial_sha256=item.partial_sha256,
        keep=item.keep,
        keep_policy=item.keep_policy,
        keep_reason=item.keep_reason,
        members=item.members,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _iso(value: int) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat(timespec="seconds")


def _iso_optional(value: int | None) -> str | None:
    return None if value is None else _iso(value)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _group_header(group: DuplicateGroup) -> str:
    text = (
        f"{group.group_id}  {format_bytes(group.size)} each, "
        f"{_plural(group.copies, 'copy', 'copies')}, reclaimable "
        f"{format_bytes(group.reclaimable_bytes)}  [sha256 {group.sha256[:12]}]"
    )
    if group.paths > group.copies:
        text += (
            f"  ({_plural(group.paths, 'path')}; "
            f"{format_bytes(group.linked_bytes)} already saved by hardlinks)"
        )
    return text


def _member_line(member: GroupMember) -> str:
    line = f"  {member.role:<9} {format_bytes(member.size):>9}  {member.path}"
    if member.same_as:
        line += f"  (same file as {member.same_as})"
    elif member.links > 1:
        line += f"  ({_plural(member.links, 'hardlink')})"
    return line


def render_text(report: ScanReport, *, top: int = DEFAULT_TOP) -> str:
    """Render the report as compact plain text, biggest reclaim first."""
    if top < 0:
        raise DeepScanError(f"top must not be negative, got {top}")
    summary = report.summary
    lines = [
        f"roots: {', '.join(report.roots)}",
        f"as of: {_iso(report.as_of)}, {report.elapsed_s:.2f} s",
        f"min size: {format_bytes(report.min_size)}, partial read "
        f"{format_bytes(report.partial_bytes)}",
        f"scanned: {_plural(report.stats.files, 'file')}, "
        f"{format_bytes(report.stats.bytes)} (skipped {report.stats.skipped_small} small, "
        f"{report.stats.skipped_links} links, {report.stats.skipped_special} special)",
        f"candidates: {_plural(report.stats.candidates, 'file')}, "
        f"{format_bytes(report.stats.candidate_bytes)}",
        f"hashed: {_plural(report.stats.partial_reads, 'partial read')} + "
        f"{_plural(report.stats.full_reads, 'full read')}, "
        f"{format_bytes(report.stats.bytes_read)} read",
    ]
    if report.stats.reused_reads:
        lines.append(
            f"reused: {_plural(report.stats.reused_reads, 'path')} hashed by file "
            f"identity (hard links are not read twice)"
        )
    if report.stats.unreadable or report.stats.changed:
        lines.append(
            f"dropped: {report.stats.unreadable} unreadable, "
            f"{report.stats.changed} changed while scanning"
        )
    lines.append(
        f"groups: {_plural(summary.groups, 'duplicate group')} "
        f"({_plural(summary.paths, 'path')}, {_plural(summary.copies, 'copy', 'copies')}) "
        f"-- reclaimable {format_bytes(summary.reclaimable_bytes)}"
    )
    if summary.hardlink_sets:
        lines.append(
            f"hardlinks: {_plural(summary.hardlink_sets, 'set')} "
            f"({_plural(summary.hardlink_paths, 'path')}) -- "
            f"{format_bytes(summary.linked_bytes)} already saved"
        )
    for note in report.notes:
        lines.append(f"note: {note}")
    listed = report.groups if top == 0 else report.groups[:top]
    for group in listed:
        lines.append(f"\n{_group_header(group)}")
        lines.append(f"  keep: {group.keep}  ({group.keep_reason})")
        for member in group.members:
            if member.role == ROLE_KEEP:
                continue
            lines.append(_member_line(member))
        if group.hardlink.feasible:
            count = len(group.hardlink.relink)
            noun = "a hardlink" if count == 1 else "hardlinks"
            paths = _plural(count, "path")
            lines.append(
                f"  dedupe: re-create {paths} as {noun} to "
                f"{group.hardlink.link_to} (same volume) -- frees "
                f"{format_bytes(group.hardlink.reclaimable_bytes)}, every path stays valid"
            )
        else:
            lines.append(f"  dedupe: {group.hardlink.reason}")
    if len(report.groups) > len(listed):
        lines.append(f"\n({len(report.groups) - len(listed)} more groups; --top 0 lists every one)")
    for item in report.hardlink_sets:
        lines.append(
            f"\n{item.set_id}  {format_bytes(item.size)} each, "
            f"{_plural(item.paths, 'path')}, one physical copy -- "
            f"{format_bytes(item.linked_bytes)} already saved, nothing to reclaim"
        )
        lines.append(f"  keep: {item.keep}  ({item.keep_reason})")
        for member in item.members:
            lines.append(_member_line(member))
    if report.stats.errors:
        lines.append(f"\nerrors ({report.stats.errors}):")
        for issue in report.stats.error_samples:
            lines.append(f"  {issue.path}: {issue.reason}")
        extra = report.stats.errors - len(report.stats.error_samples)
        if extra > 0:
            lines.append(f"  (+{extra} more)")
    return "\n".join(lines) + "\n"


def render_json(report: ScanReport) -> str:
    """Render the report as pretty-printed JSON (``spacesage.deepscan/v1``)."""
    return json.dumps(report.to_dict(), indent=2) + "\n"


__all__ = [
    "DEFAULT_MIN_SIZE",
    "DEFAULT_TOP",
    "KEEP_POLICY",
    "MAX_ERROR_SAMPLES",
    "PARTIAL_BYTES",
    "ROLES",
    "ROLE_DUPLICATE",
    "ROLE_HARDLINK",
    "ROLE_KEEP",
    "SCHEMA",
    "DeepScanError",
    "DuplicateGroup",
    "GroupMember",
    "HardlinkPlan",
    "HardlinkSet",
    "ScanIssue",
    "ScanProgress",
    "ScanReport",
    "ScanStats",
    "ScanSummary",
    "hardlink_dedupe",
    "render_json",
    "render_text",
    "scan",
]
