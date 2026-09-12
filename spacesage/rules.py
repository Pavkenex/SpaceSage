"""Rule packs (TOML) and the classifier that turns entries into categories.

A *rule pack* is a TOML file with an optional ``[pack]`` table and one or more
``[[rule]]`` tables (``docs/design.md`` section 6, ``docs/rules.md`` for the
authoring guide).  Every rule classifies an entry -- category, risk tier,
suggested action, confidence and a plain-language rationale:

    [[rule]]
    id = "pip-cache"
    path = ["**/AppData/Local/pip/Cache/**", "**/.cache/pip/**"]
    category = "dev-cache"
    tier = "T1"
    action = "DELETE_QUARANTINE"
    confidence = 0.95
    rationale = "pip download/build cache; regenerates automatically."
    native = "pip cache purge"

Matching (first match wins, **in rule order** -- see :func:`load_rules`):

* ``path`` -- glob list, OR-ed together.  ``*`` and ``?`` never cross a path
  separator, ``**`` does; a trailing ``/**`` also matches the folder itself
  (``**/Temp/**`` covers ``C:\\Windows\\Temp`` and everything below it).
  Matching is case-insensitive for Windows-style paths (drive letter or UNC
  prefix -- Windows filenames are case-insensitive) and case-sensitive for
  POSIX-style paths.
* ``ext`` -- file-extension list (with or without the dot), OR-ed; folders
  never match (their extension is ``NULL``).
* ``min_size`` -- bytes or a suffixed size (``"10 MiB"``); folders compare
  their file-row subtree size.
* ``older_than_days`` -- matches only entries whose ``mtime`` is known and at
  least that old (an unknown timestamp never matches).
* ``name_regex`` -- ``re.search`` against the last path component, compiled
  case-insensitively for Windows-style paths.

Different matcher kinds are AND-ed; at least one matcher is required.  Entries
no rule matches are classified as :data:`UNKNOWN_CATEGORY` (tier ``T3``,
action ``REVIEW``) -- never destructive.

Built-in packs live in ``spacesage/rules/``; user packs live in
``~/.config/spacesage/rules/`` (``%APPDATA%\\spacesage\\rules`` on Windows,
``$SPACESAGE_RULES_DIR`` overrides both).  Pack priority is ``(order, pack id)``
and user rules are tried before built-in rules, so a user pack can both
*shadow* a built-in rule (same ``id`` -- it is replaced in place, keeping the
built-in's position) and carve exceptions out of broad built-in patterns.

The classification is derived data: :func:`build_categories` materialises it
into the ``categories`` table (schema v3) for later stages, while
:func:`classify_report` recomputes it from ``entries`` on demand so the CLI
stays read-only.
"""

from __future__ import annotations

import heapq
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from spacesage import db, stats

BUILTIN_RULES_DIR = Path(__file__).resolve().parent / "rules"
"""Directory holding the built-in rule packs shipped with the package."""

RULES_ENV_VAR = "SPACESAGE_RULES_DIR"
"""Environment variable overriding the user rule-pack directory."""

DEFAULT_TOP = 20
"""Rows per ranked list in :func:`classify_report`."""

TIERS: tuple[str, ...] = ("T1", "T2", "T3")
"""Risk tiers: T1 disposable caches, T2 app-owned data, T3 report-only."""

ACTIONS: tuple[str, ...] = (
    "DELETE_QUARANTINE",
    "MOVE",
    "COMPRESS_NTFS",
    "REVIEW",
    "NATIVE",
    "KEEP",
)
"""Action vocabulary; ``KEEP`` is the explicit "No action" answer."""

UNKNOWN_CATEGORY = "unknown"
"""Category for entries no rule matched."""

UNKNOWN_TIER = "T3"
"""Tier of the fallback classification (report-only)."""

UNKNOWN_ACTION = "REVIEW"
"""Action of the fallback classification (never destructive)."""

UNKNOWN_RATIONALE = (
    "No rule matched this entry: SpaceSage has no advice for it, so it stays "
    "untouched until you review it yourself."
)

_INSERT_BATCH = 10_000
_DAY_SECONDS = 86_400
_RULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DRIVE_PREFIX_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[A-Za-z]:$|\\\\)")
_TRAILING_SEPARATOR_RE = re.compile(r"^[A-Za-z]:[\\/]$")
_SIZE_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)$")
_SIZE_UNITS: Mapping[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
    "pib": 1024**5,
}

_PACK_KEYS = frozenset({"id", "title", "order"})
_RULE_KEYS = frozenset(
    {
        "id",
        "path",
        "ext",
        "min_size",
        "older_than_days",
        "name_regex",
        "category",
        "tier",
        "action",
        "confidence",
        "rationale",
        "native",
    }
)
_REQUIRED_RULE_KEYS = ("category", "tier", "action", "confidence", "rationale")

_FILES_SQL = """
SELECT id, path, name, size, mtime, ext, hardlink_flag
FROM entries
WHERE is_dir = 0
"""

_CATEGORY_COLUMNS = (
    "entry_id, pack, rule_id, category, tier, action, confidence, rationale, native, bytes, is_dir"
)


class RulesError(RuntimeError):
    """Raised when a rule pack cannot be loaded or a classification cannot run."""


# --------------------------------------------------------------------------- #
# Globs and sizes
# --------------------------------------------------------------------------- #


def glob_to_regex(pattern: str) -> str:
    """Translate a path glob into an anchored regular expression.

    ``*``/``?`` do not cross separators, ``**`` does, ``**/`` matches zero or
    more leading components, and a trailing ``/**`` also matches the folder
    itself.  Both separators are accepted in patterns and in paths.
    """
    if pattern.endswith("/**"):
        prefix = pattern[: -len("/**")]
        tail = "(?:[\\\\/].*)?$"
        if not prefix:
            return "^.*$"
        pattern = prefix
    else:
        tail = "$"

    out: list[str] = ["^"]
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern[index : index + 2] == "**":
                after = index + 2
                if after < length and pattern[after] in "\\/":
                    out.append("(?:.*[\\\\/])?")
                    index = after + 1
                else:
                    out.append(".*")
                    index = after
            else:
                out.append("[^\\\\/]*")
                index += 1
        elif char == "?":
            out.append("[^\\\\/]")
            index += 1
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out.append(re.escape(char))
                index += 1
                continue
            body = pattern[index + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = end + 1
        elif char in "\\/":
            out.append("[\\\\/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append(tail)
    return "".join(out)


def parse_size(value: object) -> int:
    """Parse a byte count: an integer or a suffixed string such as ``"10 MiB"``.

    Suffixes are binary (``KB`` and ``KiB`` both mean 1024 bytes) and may be
    written case-insensitively, with or without a space.
    """
    if isinstance(value, bool):
        raise RulesError(f"{value!r} is not a size")
    if isinstance(value, int):
        if value < 0:
            raise RulesError(f"size {value} must not be negative")
        return value
    if isinstance(value, float):
        if value < 0:
            raise RulesError(f"size {value} must not be negative")
        return int(value)
    if isinstance(value, str):
        match = _SIZE_RE.match(value.strip())
        if match is None:
            raise RulesError(
                f"{value!r} is not a size; write bytes or a suffix like '10 MiB', '500 KB'"
            )
        unit = match.group("unit").lower()
        factor = _SIZE_UNITS.get(unit)
        if factor is None:
            allowed = ", ".join(sorted(unit + "iB" for unit in ("K", "M", "G", "T", "P")))
            raise RulesError(f"unknown size unit {match.group('unit')!r}; use {allowed} (or 'B')")
        return int(float(match.group("value")) * factor)
    raise RulesError(f"{value!r} is not a size")


def _windows_style(path: str) -> bool:
    """True for drive-letter and UNC paths (matched case-insensitively)."""
    return _DRIVE_PREFIX_RE.match(path) is not None


def _match_key(path: str) -> str:
    """Normalised lookup key: lower-cased, single-separator form of ``path``."""
    return path.lower().replace("/", "\\")


def _path_components(pattern: str) -> set[str]:
    """Lower-cased literal components of a glob (wildcard parts dropped)."""
    out: set[str] = set()
    for part in re.split(r"[\\/]", pattern):
        if not part or "*" in part or "?" in part or "[" in part:
            continue
        out.add(part.lower())
    return out


def _required_components(paths: Sequence[str]) -> tuple[frozenset[str], ...]:
    """Literal path components each pattern needs, one set per pattern.

    Used as a prefilter: a path missing a component of a pattern cannot match
    that pattern, so a rule is only a candidate when at least one set is
    contained in the entry's components.  An empty set means "no information".
    """
    return tuple(frozenset(_path_components(pattern)) for pattern in paths)


# --------------------------------------------------------------------------- #
# Rules and packs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntryFacts:
    """The entry attributes the matcher looks at (files and folders alike)."""

    path: str
    name: str
    is_dir: bool
    size: int
    ext: str | None = None
    mtime: int | None = None


@dataclass(frozen=True)
class Rule:
    """One classification rule, as authored in a pack."""

    id: str
    pack: str
    source: str
    category: str
    tier: str
    action: str
    confidence: float
    rationale: str
    native: str | None
    paths: tuple[str, ...]
    exts: frozenset[str]
    min_size: int | None
    older_than_days: float | None
    name_pattern: str | None
    path_required: tuple[frozenset[str], ...] = ()
    anchors: tuple[str, ...] = ()
    path_res: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)
    path_res_ci: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)
    name_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)
    name_re_ci: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def matches(self, entry: EntryFacts, *, now: float | None = None) -> bool:
        """True when every matcher of this rule accepts ``entry``."""
        return self._matches(entry, now=time.time() if now is None else now)

    def _matches(self, entry: EntryFacts, *, now: float) -> bool:
        """Full check; the caller may pass pre-computed path facts for speed."""
        key = _match_key(entry.path)
        return self._check(
            entry,
            now=now,
            windows=_windows_style(entry.path),
            key=key,
            parts=frozenset(key.split("\\")),
        )

    def _check(
        self,
        entry: EntryFacts,
        *,
        now: float,
        windows: bool,
        key: str,
        parts: frozenset[str],
    ) -> bool:
        if self.path_required and not any(required <= parts for required in self.path_required):
            return False
        if self.min_size is not None and entry.size < self.min_size:
            return False
        if self.older_than_days is not None and (
            entry.mtime is None or (now - entry.mtime) < self.older_than_days * _DAY_SECONDS
        ):
            return False
        if self.exts and (entry.ext is None or entry.ext not in self.exts):
            return False
        if self.name_pattern is not None:
            name_re = self.name_re_ci if windows else self.name_re
            if name_re is not None and name_re.search(entry.name) is None:
                return False
        if self.path_res:
            if windows:
                path = key
                patterns = self.path_res_ci
            else:
                path = entry.path
                patterns = self.path_res
            if not any(pattern.search(path) for pattern in patterns):
                return False
        return True

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view of the rule (used by ``--list-rules``)."""
        return {
            "id": self.id,
            "pack": self.pack,
            "source": self.source,
            "category": self.category,
            "tier": self.tier,
            "action": self.action,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "native": self.native,
            "path": list(self.paths),
            "ext": sorted(self.exts),
            "min_size": self.min_size,
            "older_than_days": self.older_than_days,
            "name_regex": self.name_pattern,
        }


@dataclass(frozen=True)
class RulePack:
    """One loaded TOML pack."""

    id: str
    title: str
    order: int
    source: str
    rules: tuple[Rule, ...]
    user: bool = False


@dataclass(frozen=True)
class RuleSummary:
    """Headline numbers about the loaded rule set."""

    rules: int
    builtin_packs: tuple[str, ...]
    user_packs: tuple[str, ...]
    shadowed: tuple[str, ...]
    categories: int
    fingerprint: str


@dataclass(frozen=True)
class Classification:
    """The verdict for one entry (rule-based, or the ``unknown`` fallback)."""

    entry_id: int
    path: str
    is_dir: bool
    size: int
    category: str
    tier: str
    action: str
    confidence: float
    rationale: str
    rule_id: str | None = None
    pack: str | None = None
    native: str | None = None
    hardlink: bool = False
    mtime: int | None = None
    """The entry's ``Modified`` epoch (``None`` when the export had none)."""

    @property
    def matched(self) -> bool:
        """True when a rule produced this verdict (``unknown`` when it did not)."""
        return self.rule_id is not None


# --------------------------------------------------------------------------- #
# Pack parsing and loading
# --------------------------------------------------------------------------- #


def _label(source: str, index: int, rule_id: str | None) -> str:
    where = f"{source}: [[rule]] #{index}"
    return f"{where} ({rule_id!r})" if rule_id else where


def _parse_string_list(raw: object, *, where: str, key: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        values: list[object] = [raw]
    elif isinstance(raw, list):
        values = list(raw)
    else:
        raise RulesError(f"{where}: {key!r} must be a string or a list of strings")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise RulesError(f"{where}: {key!r} must hold non-empty strings ({value!r})")
        out.append(value.strip())
    if not out:
        raise RulesError(f"{where}: {key!r} must not be empty")
    return tuple(out)


def _parse_rule(raw: object, *, source: str, index: int, pack_id: str) -> Rule:
    if not isinstance(raw, dict):
        raise RulesError(f"{source}: [[rule]] #{index} must be a table")
    table: dict[str, Any] = {str(key): value for key, value in raw.items()}
    rule_id = table.get("id")
    if rule_id is not None and not isinstance(rule_id, str):
        raise RulesError(f"{_label(source, index, None)}: 'id' must be a string")
    where = _label(source, index, rule_id if isinstance(rule_id, str) else None)

    unknown = sorted(set(table) - _RULE_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_RULE_KEYS))
        raise RulesError(
            f"{where}: unknown key(s) {', '.join(unknown)}; allowed keys are {allowed}"
        )
    missing = [key for key in _REQUIRED_RULE_KEYS if key not in table]
    if missing:
        raise RulesError(f"{where}: missing required key(s): {', '.join(missing)}")

    rid = table.get("id")
    if rid is None:
        raise RulesError(f"{source}: [[rule]] #{index} is missing required key(s): id")
    if not isinstance(rid, str) or not _RULE_ID_RE.match(rid):
        raise RulesError(
            f"{where}: rule id must be lowercase letters, digits, '.', '_' or '-' (got {rid!r})"
        )

    category = table["category"]
    if not isinstance(category, str) or not _RULE_ID_RE.match(category):
        raise RulesError(
            f"{where}: category must be lowercase letters, digits, '.', '_' or '-' "
            f"(got {category!r})"
        )

    tier = table["tier"]
    if tier not in TIERS:
        raise RulesError(f"{where}: unknown tier {tier!r}; use one of {', '.join(TIERS)}")

    action = table["action"]
    if action not in ACTIONS:
        raise RulesError(f"{where}: unknown action {action!r}; use one of {', '.join(ACTIONS)}")

    confidence = table["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise RulesError(f"{where}: 'confidence' must be a number between 0 and 1")
    if not 0.0 <= float(confidence) <= 1.0:
        raise RulesError(f"{where}: 'confidence' must be between 0 and 1 (got {confidence})")

    rationale = table["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise RulesError(f"{where}: 'rationale' must be a non-empty sentence")
    rationale = rationale.strip()

    native = table.get("native")
    if native is not None and (not isinstance(native, str) or not native.strip()):
        raise RulesError(f"{where}: 'native' must be a non-empty string when present")
    if action == "NATIVE" and not native:
        raise RulesError(
            f"{where}: action 'NATIVE' needs a 'native' command the user should run instead"
        )
    native = native.strip() if isinstance(native, str) else None

    paths = _parse_string_list(table["path"], where=where, key="path") if "path" in table else ()
    for pattern in paths:
        if _TRAILING_SEPARATOR_RE.match(pattern):
            continue
        if pattern.endswith("\\") or pattern.endswith("/"):
            fixed = pattern.rstrip("\\/") + "/**"
            raise RulesError(
                f"{where}: path pattern {pattern!r} must not end with a separator; "
                f"write {fixed!r} to cover a folder and its contents"
            )

    exts: tuple[str, ...] = ()
    if "ext" in table:
        raw_exts = _parse_string_list(table["ext"], where=where, key="ext")
        exts = tuple(dict.fromkeys(item.lower().lstrip(".") for item in raw_exts))
        if any(not item for item in exts):
            raise RulesError(f"{where}: 'ext' entries must not be empty (drop the dot)")

    min_size: int | None = None
    if "min_size" in table:
        try:
            min_size = parse_size(table["min_size"])
        except RulesError as exc:
            raise RulesError(f"{where}: 'min_size' {exc}") from None

    older_than_days: float | None = None
    if "older_than_days" in table:
        raw_age = table["older_than_days"]
        if isinstance(raw_age, bool) or not isinstance(raw_age, (int, float)) or raw_age < 0:
            raise RulesError(f"{where}: 'older_than_days' must be a non-negative number")
        older_than_days = float(raw_age)

    name_pattern: str | None = None
    name_re: re.Pattern[str] | None = None
    name_re_ci: re.Pattern[str] | None = None
    if "name_regex" in table:
        name_pattern = table["name_regex"]
        if not isinstance(name_pattern, str) or not name_pattern:
            raise RulesError(f"{where}: 'name_regex' must be a non-empty regular expression")
        try:
            name_re = re.compile(name_pattern)
            name_re_ci = re.compile(name_pattern, re.IGNORECASE)
        except re.error as exc:
            raise RulesError(f"{where}: invalid 'name_regex' {name_pattern!r}: {exc}") from None

    if not (paths or exts or min_size is not None or older_than_days is not None or name_pattern):
        raise RulesError(
            f"{where}: the rule has no matcher; add at least one of "
            f"path, ext, min_size, older_than_days, name_regex"
        )

    path_res: list[re.Pattern[str]] = []
    path_res_ci: list[re.Pattern[str]] = []
    for pattern in paths:
        translated = glob_to_regex(pattern)
        path_res.append(re.compile(translated))
        path_res_ci.append(re.compile(translated, re.IGNORECASE))

    path_required = _required_components(paths)
    anchors = tuple(max(sorted(required), key=len) for required in path_required if required)
    return Rule(
        id=rid,
        pack=pack_id,
        source=source,
        category=category,
        tier=str(tier),
        action=str(action),
        confidence=float(confidence),
        rationale=rationale,
        native=native,
        paths=paths,
        exts=frozenset(exts),
        min_size=min_size,
        older_than_days=older_than_days,
        name_pattern=name_pattern,
        path_required=path_required,
        anchors=anchors,
        path_res=tuple(path_res),
        path_res_ci=tuple(path_res_ci),
        name_re=name_re,
        name_re_ci=name_re_ci,
    )


def parse_pack(
    text: str,
    *,
    source: str = "<pack>",
    default_id: str | None = None,
    user: bool = False,
) -> RulePack:
    """Parse one pack document; raise :class:`RulesError` with actionable detail."""
    import tomllib

    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RulesError(f"{source}: invalid TOML: {exc}") from None

    unknown_top = sorted(set(document) - {"pack", "rule"})
    if unknown_top:
        raise RulesError(
            f"{source}: unknown table(s) {', '.join(unknown_top)}; "
            f"a pack holds an optional [pack] table and [[rule]] tables"
        )

    pack_table = document.get("pack", {})
    if not isinstance(pack_table, dict):
        raise RulesError(f"{source}: [pack] must be a table")
    unknown_pack = sorted(set(pack_table) - _PACK_KEYS)
    if unknown_pack:
        raise RulesError(
            f"{source}: unknown [pack] key(s) {', '.join(unknown_pack)}; "
            f"allowed keys are {', '.join(sorted(_PACK_KEYS))}"
        )
    pack_id_raw = pack_table.get("id", default_id)
    if not isinstance(pack_id_raw, str) or not _RULE_ID_RE.match(pack_id_raw):
        raise RulesError(
            f"{source}: pack id must be lowercase letters, digits, '.', '_' or '-' "
            f"(got {pack_id_raw!r}; set [pack] id when the file name does not fit)"
        )
    title = pack_table.get("title", pack_id_raw)
    if not isinstance(title, str) or not title.strip():
        raise RulesError(f"{source}: [pack] title must be a non-empty string")
    order = pack_table.get("order", 100)
    if isinstance(order, bool) or not isinstance(order, int):
        raise RulesError(f"{source}: [pack] order must be an integer (got {order!r})")

    raw_rules = document.get("rule", [])
    if not isinstance(raw_rules, list):
        raise RulesError(f"{source}: [[rule]] must be a list of tables")
    if not raw_rules:
        raise RulesError(f"{source}: the pack defines no [[rule]] entries")

    rules = tuple(
        _parse_rule(raw, source=source, index=index, pack_id=pack_id_raw)
        for index, raw in enumerate(raw_rules, start=1)
    )
    duplicates = _duplicates(rule.id for rule in rules)
    if duplicates:
        raise RulesError(f"{source}: duplicate rule id(s) in one pack: {', '.join(duplicates)}")
    return RulePack(
        id=pack_id_raw,
        title=title.strip(),
        order=order,
        source=source,
        rules=rules,
        user=user,
    )


def load_pack(path: str | Path, *, user: bool = False) -> RulePack:
    """Load one pack file from disk."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"cannot read rule pack {target}: {exc}") from None
    return parse_pack(
        text,
        source=str(target),
        default_id=target.stem.lower().replace(" ", "-"),
        user=user,
    )


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicate: set[str] = set()
    for value in values:
        if value in seen:
            duplicate.add(value)
        seen.add(value)
    return sorted(duplicate)


def default_rules_dir(env: Mapping[str, str] | None = None) -> Path:
    """User rule-pack directory (``$SPACESAGE_RULES_DIR`` wins)."""
    environ = os.environ if env is None else env
    override = environ.get(RULES_ENV_VAR)
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base).expanduser() / "spacesage" / "rules"
    xdg = environ.get("XDG_CONFIG_HOME")
    base_dir = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base_dir / "spacesage" / "rules"


def _load_pack_dir(directory: Path, *, user: bool) -> tuple[RulePack, ...]:
    packs: list[RulePack] = []
    for path in sorted(directory.glob("*.toml")):
        packs.append(load_pack(path, user=user))
    return tuple(packs)


def load_rules(
    *,
    builtin_dir: str | Path | None = None,
    user_dir: str | Path | None = None,
    include_user: bool = True,
) -> RuleSet:
    """Load the effective rule set: user packs first, then built-in packs.

    User rules shadow built-in rules with the same ``id`` (the built-in rule is
    dropped and the user version keeps the user section's position, i.e. it is
    tried first).  Packs are ordered by ``(order, pack id)`` within each
    section, so the effective order is deterministic.
    """
    builtin_root = Path(builtin_dir) if builtin_dir is not None else BUILTIN_RULES_DIR
    if not builtin_root.is_dir():
        raise RulesError(f"built-in rule packs are missing at {builtin_root}")
    builtin_packs = _load_pack_dir(builtin_root, user=False)
    user_packs: tuple[RulePack, ...] = ()
    if include_user:
        user_root = Path(user_dir) if user_dir is not None else default_rules_dir()
        if user_root.is_dir():
            user_packs = _load_pack_dir(user_root, user=True)

    for label, packs in (("built-in", builtin_packs), ("user", user_packs)):
        clash = _duplicates(rule.id for pack in packs for rule in pack.rules)
        if clash:
            raise RulesError(
                f"duplicate rule id(s) across {label} packs: {', '.join(clash)} "
                f"(ids must be unique; use the same id only to shadow a built-in rule)"
            )

    builtin_rules = [
        rule
        for pack in sorted(builtin_packs, key=lambda item: (item.order, item.id))
        for rule in pack.rules
    ]
    user_rules = [
        rule
        for pack in sorted(user_packs, key=lambda item: (item.order, item.id))
        for rule in pack.rules
    ]
    user_ids = {rule.id for rule in user_rules}
    shadowed = tuple(rule.id for rule in builtin_rules if rule.id in user_ids)
    effective = tuple(user_rules) + tuple(rule for rule in builtin_rules if rule.id not in user_ids)
    return RuleSet.from_rules(
        effective,
        builtin_packs=tuple(pack.id for pack in builtin_packs),
        user_packs=tuple(pack.id for pack in user_packs),
        shadowed=shadowed,
    )


@dataclass(frozen=True)
class RuleSet:
    """The effective, ordered rule list plus its matcher index.

    The index keeps per-entry matching cheap: rules that require an extension
    are filed under every extension they accept, rules with path patterns under
    an anchor component of each pattern, and only the rules without either
    (name/age/size-only rules) are checked for every entry.
    """

    rules: tuple[Rule, ...]
    builtin_packs: tuple[str, ...]
    user_packs: tuple[str, ...]
    shadowed: tuple[str, ...]
    buckets: Mapping[str, tuple[int, ...]] = field(default_factory=dict, repr=False, compare=False)
    ext_buckets: Mapping[str, tuple[int, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    always: tuple[int, ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def from_rules(
        cls,
        rules: Iterable[Rule],
        *,
        builtin_packs: Sequence[str] = (),
        user_packs: Sequence[str] = (),
        shadowed: Sequence[str] = (),
    ) -> RuleSet:
        """Build a set from parsed rules, indexing them for fast matching."""
        ordered = tuple(rules)
        anchor_buckets: dict[str, list[int]] = {}
        ext_buckets: dict[str, list[int]] = {}
        always: list[int] = []
        for index, rule in enumerate(ordered):
            if rule.exts:
                for ext in sorted(rule.exts):
                    ext_buckets.setdefault(ext, []).append(index)
            elif rule.anchors:
                for anchor in sorted(set(rule.anchors)):
                    anchor_buckets.setdefault(anchor, []).append(index)
            else:
                always.append(index)
        return cls(
            rules=ordered,
            builtin_packs=tuple(builtin_packs),
            user_packs=tuple(user_packs),
            shadowed=tuple(shadowed),
            buckets={key: tuple(value) for key, value in anchor_buckets.items()},
            ext_buckets={key: tuple(value) for key, value in ext_buckets.items()},
            always=tuple(always),
        )

    def match(self, entry: EntryFacts, *, now: float | None = None) -> Rule | None:
        """The first rule that matches ``entry``, or ``None`` (unknown)."""
        moment = time.time() if now is None else float(now)
        if not self.rules:
            return None
        key = _match_key(entry.path)
        parts = frozenset(key.split("\\"))
        candidates: set[int] = set(self.always)
        if entry.ext is not None:
            hit = self.ext_buckets.get(entry.ext)
            if hit is not None:
                candidates.update(hit)
        buckets = self.buckets
        for component in parts:
            hit = buckets.get(component)
            if hit is not None:
                candidates.update(hit)
        if not candidates:
            return None
        windows = _windows_style(entry.path)
        ordered = (next(iter(candidates)),) if len(candidates) == 1 else tuple(sorted(candidates))
        for index in ordered:
            rule = self.rules[index]
            if rule._check(entry, now=moment, windows=windows, key=key, parts=parts):
                return rule
        return None

    def classify(self, entry: EntryFacts, *, now: float | None = None) -> Classification:
        """Classify ``entry``; unmatched entries fall back to ``unknown``."""
        rule = self.match(entry, now=now)
        return self._verdict(entry, rule)

    def _verdict(
        self, entry: EntryFacts, rule: Rule | None, *, entry_id: int = 0, hardlink: bool = False
    ) -> Classification:
        if rule is None:
            return Classification(
                entry_id=entry_id,
                path=entry.path,
                is_dir=entry.is_dir,
                size=entry.size,
                category=UNKNOWN_CATEGORY,
                tier=UNKNOWN_TIER,
                action=UNKNOWN_ACTION,
                confidence=0.0,
                rationale=UNKNOWN_RATIONALE,
                hardlink=hardlink,
                mtime=entry.mtime,
            )
        return Classification(
            entry_id=entry_id,
            path=entry.path,
            is_dir=entry.is_dir,
            size=entry.size,
            category=rule.category,
            tier=rule.tier,
            action=rule.action,
            confidence=rule.confidence,
            rationale=rule.rationale,
            rule_id=rule.id,
            pack=rule.pack,
            native=rule.native,
            hardlink=hardlink,
            mtime=entry.mtime,
        )

    @property
    def fingerprint(self) -> str:
        """Stable hash of the effective rules (stored with the materialisation)."""
        payload = json.dumps([rule.to_dict() for rule in self.rules], sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()

    def summary(self) -> RuleSummary:
        """Counts and pack names for reports."""
        return RuleSummary(
            rules=len(self.rules),
            builtin_packs=self.builtin_packs,
            user_packs=self.user_packs,
            shadowed=self.shadowed,
            categories=len({rule.category for rule in self.rules}),
            fingerprint=self.fingerprint,
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-ready list of the effective rules, in match order."""
        return {
            "schema": "spacesage.rules/v1",
            "rules": [rule.to_dict() for rule in self.rules],
            "packs": {
                "builtin": list(self.builtin_packs),
                "user": list(self.user_packs),
                "shadowed": list(self.shadowed),
            },
            "fingerprint": self.fingerprint,
        }


# --------------------------------------------------------------------------- #
# Classification runs
# --------------------------------------------------------------------------- #


def iter_classifications(
    conn: sqlite3.Connection,
    rules: RuleSet,
    *,
    now: float | None = None,
) -> Iterator[Classification]:
    """Yield one :class:`Classification` per entry (files, then folders).

    File rows are classified from their own attributes; folder rows use the
    :func:`spacesage.stats.iter_dir_sizes` subtree aggregates, so folder sizes
    follow the same "file rows only" rule as every other byte total.
    """
    moment = time.time() if now is None else float(now)
    for entry_id, path, name, size, mtime, ext, hardlink in conn.execute(_FILES_SQL):
        facts = EntryFacts(
            path=str(path),
            name=str(name),
            is_dir=False,
            size=int(size),
            ext=str(ext) if ext is not None else None,
            mtime=int(mtime) if mtime is not None else None,
        )
        yield rules._verdict(
            facts,
            rules.match(facts, now=moment),
            entry_id=int(entry_id),
            hardlink=bool(hardlink),
        )

    for dir_size in stats.iter_dir_sizes(conn):
        facts = EntryFacts(
            path=dir_size.path,
            name=dir_size.name,
            is_dir=True,
            size=dir_size.bytes,
            ext=None,
            mtime=dir_size.mtime,
        )
        yield rules._verdict(facts, rules.match(facts, now=moment), entry_id=dir_size.entry_id)


@dataclass(frozen=True)
class CategorySummary:
    """Per-category totals (``*_bytes`` come from file rows only)."""

    category: str
    tier: str
    action: str
    rules: tuple[str, ...]
    entries: int
    files: int
    dirs: int
    file_bytes: int
    unique_file_bytes: int
    dir_bytes: int


@dataclass(frozen=True)
class TierSummary:
    """Per-tier totals."""

    tier: str
    entries: int
    files: int
    dirs: int
    categories: int
    file_bytes: int
    unique_file_bytes: int
    dir_bytes: int


@dataclass(frozen=True)
class UnknownEntry:
    """One unmatched entry (files and folders alike)."""

    path: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class ClassifyTotals:
    """Index-wide outcome of one classification pass."""

    entries: int
    files: int
    dirs: int
    matched: int
    matched_files: int
    matched_file_bytes: int
    matched_unique_file_bytes: int
    unknown: int
    unknown_files: int
    unknown_file_bytes: int
    unknown_unique_file_bytes: int
    unknown_dir_bytes: int

    @property
    def matched_ratio(self) -> float:
        """Share of entries a rule matched (0-1)."""
        return self.matched / self.entries if self.entries else 0.0

    @property
    def unknown_bytes(self) -> int:
        """Matched-folder-free bytes: unmatched file bytes plus folder subtrees."""
        return self.unknown_file_bytes + self.unknown_dir_bytes


@dataclass(frozen=True)
class ClassifyReport:
    """Every view the ``classify`` CLI prints, in one object."""

    generated: datetime
    db_path: str | None
    schema_version: int
    rules: RuleSummary
    totals: ClassifyTotals
    tiers: tuple[TierSummary, ...]
    categories: tuple[CategorySummary, ...]
    total_categories: int
    unknown: tuple[UnknownEntry, ...]
    top: int

    def to_dict(self) -> dict[str, object]:
        """JSON-ready mapping (``spacesage.classify/v1``)."""
        totals = self.totals
        return {
            "schema": "spacesage.classify/v1",
            "generated": _iso(self.generated),
            "index": {"db": self.db_path, "schema_version": self.schema_version},
            "rules": {
                "count": self.rules.rules,
                "builtin_packs": list(self.rules.builtin_packs),
                "user_packs": list(self.rules.user_packs),
                "shadowed": list(self.rules.shadowed),
                "categories": self.rules.categories,
                "fingerprint": self.rules.fingerprint,
            },
            "totals": {
                "entries": totals.entries,
                "files": totals.files,
                "dirs": totals.dirs,
                "matched": totals.matched,
                "matched_files": totals.matched_files,
                "matched_file_bytes": totals.matched_file_bytes,
                "matched_unique_file_bytes": totals.matched_unique_file_bytes,
                "unknown": totals.unknown,
                "unknown_files": totals.unknown_files,
                "unknown_file_bytes": totals.unknown_file_bytes,
                "unknown_unique_file_bytes": totals.unknown_unique_file_bytes,
                "unknown_dir_bytes": totals.unknown_dir_bytes,
                "unknown_bytes": totals.unknown_bytes,
                "matched_ratio": round(totals.matched_ratio, 6),
            },
            "top": self.top,
            "tiers": [
                {
                    "tier": tier.tier,
                    "entries": tier.entries,
                    "files": tier.files,
                    "dirs": tier.dirs,
                    "categories": tier.categories,
                    "file_bytes": tier.file_bytes,
                    "unique_file_bytes": tier.unique_file_bytes,
                    "dir_bytes": tier.dir_bytes,
                }
                for tier in self.tiers
            ],
            "categories": {
                "listed": len(self.categories),
                "total": self.total_categories,
                "items": [
                    {
                        "category": item.category,
                        "tier": item.tier,
                        "action": item.action,
                        "rules": list(item.rules),
                        "entries": item.entries,
                        "files": item.files,
                        "dirs": item.dirs,
                        "file_bytes": item.file_bytes,
                        "unique_file_bytes": item.unique_file_bytes,
                        "dir_bytes": item.dir_bytes,
                    }
                    for item in self.categories
                ],
            },
            "unknown": {
                "listed": len(self.unknown),
                "items": [
                    {"path": item.path, "is_dir": item.is_dir, "size": item.size}
                    for item in self.unknown
                ],
            },
        }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class _TopEntries:
    """Bounded heap of the ``limit`` largest entries seen so far (streaming)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._heap: list[tuple[int, int, UnknownEntry]] = []
        self._sequence = 0

    def offer(self, entry: UnknownEntry) -> None:
        """Consider one entry, keeping memory at ``limit`` items."""
        if self._limit <= 0:
            return
        item = (entry.size, self._sequence, entry)
        self._sequence += 1
        if len(self._heap) < self._limit:
            heapq.heappush(self._heap, item)
        elif item > self._heap[0]:
            heapq.heapreplace(self._heap, item)

    @property
    def items(self) -> tuple[UnknownEntry, ...]:
        """The kept entries, largest first (ties in encounter order)."""
        ranked = sorted(self._heap, key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked)


@dataclass(slots=True)
class _CategoryAccumulator:
    """Running totals for one ``(category, tier, action)`` group."""

    category: str
    tier: str
    action: str
    entries: int = 0
    files: int = 0
    dirs: int = 0
    file_bytes: int = 0
    unique_file_bytes: int = 0
    dir_bytes: int = 0
    rules: set[str] = field(default_factory=set)

    def add(self, classification: Classification) -> None:
        self.entries += 1
        if classification.rule_id is not None:
            self.rules.add(classification.rule_id)
        if classification.is_dir:
            self.dirs += 1
            self.dir_bytes += classification.size
            return
        self.files += 1
        self.file_bytes += classification.size
        if not classification.hardlink:
            self.unique_file_bytes += classification.size

    def summary(self) -> CategorySummary:
        return CategorySummary(
            category=self.category,
            tier=self.tier,
            action=self.action,
            rules=tuple(sorted(self.rules)),
            entries=self.entries,
            files=self.files,
            dirs=self.dirs,
            file_bytes=self.file_bytes,
            unique_file_bytes=self.unique_file_bytes,
            dir_bytes=self.dir_bytes,
        )


@dataclass(slots=True)
class _TierAccumulator:
    """Running totals for one tier."""

    tier: str
    entries: int = 0
    files: int = 0
    dirs: int = 0
    file_bytes: int = 0
    unique_file_bytes: int = 0
    dir_bytes: int = 0
    categories: set[str] = field(default_factory=set)

    def add(self, classification: Classification) -> None:
        self.entries += 1
        self.categories.add(classification.category)
        if classification.is_dir:
            self.dirs += 1
            self.dir_bytes += classification.size
            return
        self.files += 1
        self.file_bytes += classification.size
        if not classification.hardlink:
            self.unique_file_bytes += classification.size

    def summary(self) -> TierSummary:
        return TierSummary(
            tier=self.tier,
            entries=self.entries,
            files=self.files,
            dirs=self.dirs,
            categories=len(self.categories),
            file_bytes=self.file_bytes,
            unique_file_bytes=self.unique_file_bytes,
            dir_bytes=self.dir_bytes,
        )


@dataclass(slots=True)
class _TotalsAccumulator:
    """Index-wide counters for :func:`classify_report`."""

    entries: int = 0
    files: int = 0
    dirs: int = 0
    matched: int = 0
    matched_files: int = 0
    matched_file_bytes: int = 0
    matched_unique_file_bytes: int = 0
    unknown: int = 0
    unknown_files: int = 0
    unknown_file_bytes: int = 0
    unknown_unique_file_bytes: int = 0
    unknown_dir_bytes: int = 0

    def add(self, classification: Classification) -> None:
        self.entries += 1
        if classification.is_dir:
            self.dirs += 1
        else:
            self.files += 1
        if classification.rule_id is not None:
            self.matched += 1
            if not classification.is_dir:
                self.matched_files += 1
                self.matched_file_bytes += classification.size
                if not classification.hardlink:
                    self.matched_unique_file_bytes += classification.size
            return
        self.unknown += 1
        if classification.is_dir:
            self.unknown_dir_bytes += classification.size
            return
        self.unknown_files += 1
        self.unknown_file_bytes += classification.size
        if not classification.hardlink:
            self.unknown_unique_file_bytes += classification.size

    def summary(self) -> ClassifyTotals:
        return ClassifyTotals(
            entries=self.entries,
            files=self.files,
            dirs=self.dirs,
            matched=self.matched,
            matched_files=self.matched_files,
            matched_file_bytes=self.matched_file_bytes,
            matched_unique_file_bytes=self.matched_unique_file_bytes,
            unknown=self.unknown,
            unknown_files=self.unknown_files,
            unknown_file_bytes=self.unknown_file_bytes,
            unknown_unique_file_bytes=self.unknown_unique_file_bytes,
            unknown_dir_bytes=self.unknown_dir_bytes,
        )


def classify_report(
    conn: sqlite3.Connection,
    rules: RuleSet,
    *,
    top: int = DEFAULT_TOP,
    now: float | None = None,
    db_path: str | None = None,
) -> ClassifyReport:
    """Classify the whole index and aggregate per tier and category."""
    if top < 1:
        raise RulesError(f"--top must be >= 1, got {top}")
    totals = _TotalsAccumulator()
    categories: dict[tuple[str, str, str], _CategoryAccumulator] = {}
    tiers: dict[str, _TierAccumulator] = {}
    unknown_top = _TopEntries(top)
    moment = time.time() if now is None else float(now)
    if db.entry_count(conn) == 0:
        raise RulesError("the index is empty; ingest a WizTree export first")

    for classification in iter_classifications(conn, rules, now=moment):
        totals.add(classification)
        key = (classification.category, classification.tier, classification.action)
        accumulator = categories.get(key)
        if accumulator is None:
            accumulator = _CategoryAccumulator(*key)
            categories[key] = accumulator
        accumulator.add(classification)
        tier_accumulator = tiers.get(classification.tier)
        if tier_accumulator is None:
            tier_accumulator = _TierAccumulator(classification.tier)
            tiers[classification.tier] = tier_accumulator
        tier_accumulator.add(classification)
        if classification.rule_id is None:
            unknown_top.offer(
                UnknownEntry(
                    path=classification.path,
                    is_dir=classification.is_dir,
                    size=classification.size,
                )
            )

    summaries = sorted(
        (item.summary() for item in categories.values()),
        key=lambda item: (-item.file_bytes, -item.dir_bytes, item.category),
    )
    tier_summaries = tuple(tiers[tier].summary() for tier in TIERS if tier in tiers) + tuple(
        tiers[tier].summary() for tier in sorted(tiers) if tier not in TIERS
    )
    return ClassifyReport(
        generated=datetime.now(tz=UTC),
        db_path=db_path,
        schema_version=db.schema_version(conn),
        rules=rules.summary(),
        totals=totals.summary(),
        tiers=tier_summaries,
        categories=tuple(summaries[:top]),
        total_categories=len(summaries),
        unknown=unknown_top.items,
        top=top,
    )


# --------------------------------------------------------------------------- #
# Materialisation (schema v3 table)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CategoryBuild:
    """Result of :func:`build_categories`."""

    entries: int
    matched: int
    unknown: int
    built_at: datetime
    rules_sha256: str


def build_categories(
    conn: sqlite3.Connection,
    rules: RuleSet,
    *,
    now: float | None = None,
) -> CategoryBuild:
    """Rebuild the materialised ``categories`` table from the index.

    Derived data: the table is cleared and rewritten in one transaction (rows
    also disappear with their entries through the foreign key cascade), so
    later stages can join classifications without re-running the matcher.
    """
    moment = time.time() if now is None else float(now)
    built_at = datetime.now(tz=UTC)
    entries = 0
    matched = 0
    batch: list[tuple[object, ...]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM categories")
        for classification in iter_classifications(conn, rules, now=moment):
            entries += 1
            if classification.rule_id is not None:
                matched += 1
            batch.append(
                (
                    classification.entry_id,
                    classification.pack,
                    classification.rule_id,
                    classification.category,
                    classification.tier,
                    classification.action,
                    classification.confidence,
                    classification.rationale,
                    classification.native,
                    classification.size,
                    1 if classification.is_dir else 0,
                )
            )
            if len(batch) >= _INSERT_BATCH:
                _insert_categories(conn, batch)
                batch.clear()
        if batch:
            _insert_categories(conn, batch)
            batch.clear()
        fingerprint = rules.fingerprint
        db.meta_set_many(
            conn,
            {
                "classify.built_entries": entries,
                "classify.built_at": _iso(built_at),
                "classify.matched": matched,
                "classify.unknown": entries - matched,
                "classify.rules_sha256": fingerprint,
            },
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return CategoryBuild(
        entries=entries,
        matched=matched,
        unknown=entries - matched,
        built_at=built_at,
        rules_sha256=fingerprint,
    )


def _insert_categories(conn: sqlite3.Connection, rows: list[tuple[object, ...]]) -> None:
    conn.executemany(
        f"INSERT INTO categories({_CATEGORY_COLUMNS}) VALUES ({', '.join('?' * 11)})",
        rows,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _section(title: str) -> str:
    return f"\n{title}:"


def render_rules(rules: RuleSet) -> str:
    """Render the effective rule order (``classify --list-rules``)."""
    packs = f"{len(rules.builtin_packs)} built-in"
    if rules.user_packs:
        packs += f" + {len(rules.user_packs)} user"
    lines = [f"rules: {_plural(len(rules.rules), 'rule')} ({packs} packs)"]
    if rules.shadowed:
        lines.append(f"shadowed: {', '.join(rules.shadowed)}")
    lines.append("")
    lines.append(f"  {'id':<28} {'tier':<4} {'category':<22} {'action':<18} matchers")
    for rule in rules.rules:
        matchers: list[str] = []
        if rule.paths:
            matchers.append(" | ".join(rule.paths))
        if rule.exts:
            matchers.append("ext=" + ",".join(sorted(rule.exts)))
        if rule.min_size is not None:
            matchers.append(f"min_size={stats.format_bytes(rule.min_size)}")
        if rule.older_than_days is not None:
            matchers.append(f"older_than_days={rule.older_than_days:g}")
        if rule.name_pattern is not None:
            matchers.append(f"name_regex={rule.name_pattern}")
        lines.append(
            f"  {rule.id:<28} {rule.tier:<4} {rule.category:<22} {rule.action:<18} "
            f"{'; '.join(matchers)}"
        )
    return "\n".join(lines) + "\n"


def render_text(report: ClassifyReport) -> str:
    """Render the report as compact plain text."""
    rules = report.rules
    totals = report.totals
    packs = f"{_plural(len(rules.builtin_packs), 'built-in pack')}"
    if rules.user_packs:
        packs += f" + {_plural(len(rules.user_packs), 'user pack')}"
    lines = [
        f"index: {report.db_path or '(index)'} (schema v{report.schema_version})",
        f"rules: {_plural(rules.rules, 'rule')} from {packs} "
        f"({_plural(rules.categories, 'category', 'categories')})",
    ]
    if rules.shadowed:
        lines.append(
            f"shadowed: {_plural(len(rules.shadowed), 'built-in rule')} replaced by user rules: "
            f"{', '.join(rules.shadowed)}"
        )
    lines.append(
        f"entries: {_plural(totals.entries, 'entry', 'entries')} classified "
        f"({_plural(totals.files, 'file')}, {_plural(totals.dirs, 'dir')}): "
        f"{totals.matched} matched ({totals.matched_ratio:.1%}), "
        f"{_plural(totals.unknown, 'entry', 'entries')} unknown"
    )
    lines.append(
        f"unknown: {stats.format_bytes(totals.unknown_bytes)} across "
        f"{_plural(totals.unknown, 'entry', 'entries')} "
        f"({stats.format_bytes(totals.unknown_file_bytes)} in files)"
    )

    lines.append(_section("tiers"))
    for tier in report.tiers:
        line = (
            f"  {tier.tier:<3} {_plural(tier.entries, 'entry', 'entries'):>14}  "
            f"{_plural(tier.files, 'file'):>10}  {_plural(tier.dirs, 'dir'):>8}  "
            f"{stats.format_bytes(tier.file_bytes):>10}"
        )
        if tier.dir_bytes:
            line += f"  (+{stats.format_bytes(tier.dir_bytes)} in folders)"
        lines.append(line)

    title = f"categories ({len(report.categories)} of {report.total_categories})"
    lines.append(_section(title + " by bytes"))
    for category in report.categories:
        line = (
            f"  {stats.format_bytes(category.file_bytes):>10}  {category.category:<22} "
            f"{category.tier:<3} {category.action:<18} {_plural(category.files, 'file'):>10}  "
            f"{_plural(category.dirs, 'dir'):>8}"
        )
        if category.dir_bytes:
            line += f"  (+{stats.format_bytes(category.dir_bytes)} in folders)"
        lines.append(line)

    lines.append(_section(f"unknown entries (top {len(report.unknown)} by size)"))
    for unknown in report.unknown:
        marker = "" if unknown.path.endswith(("\\", "/")) else ("\\" if unknown.is_dir else "")
        lines.append(f"  {stats.format_bytes(unknown.size):>10}  {unknown.path}{marker}")
    return "\n".join(lines) + "\n"


def render_json(report: ClassifyReport) -> str:
    """Render the complete report as pretty-printed JSON."""
    return json.dumps(report.to_dict(), indent=2) + "\n"


__all__ = [
    "ACTIONS",
    "BUILTIN_RULES_DIR",
    "DEFAULT_TOP",
    "RULES_ENV_VAR",
    "TIERS",
    "UNKNOWN_ACTION",
    "UNKNOWN_CATEGORY",
    "UNKNOWN_RATIONALE",
    "UNKNOWN_TIER",
    "CategoryBuild",
    "CategorySummary",
    "Classification",
    "ClassifyReport",
    "ClassifyTotals",
    "EntryFacts",
    "Rule",
    "RulePack",
    "RuleSet",
    "RuleSummary",
    "RulesError",
    "TierSummary",
    "UnknownEntry",
    "build_categories",
    "classify_report",
    "default_rules_dir",
    "glob_to_regex",
    "iter_classifications",
    "load_pack",
    "load_rules",
    "parse_pack",
    "parse_size",
    "render_json",
    "render_rules",
    "render_text",
]
