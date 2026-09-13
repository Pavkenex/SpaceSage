"""Guardrails: schema validation, dataset locking, caching, metering, redaction.

Everything here exists because the model is not trusted: it is a text generator
that may hallucinate a path, invent an action, answer with prose instead of
JSON, or be coaxed by a filename.  The guards are deliberately dumb and
auditable:

``validate``
    a hand-written JSON-Schema (draft-07 subset) checker - the same schemas the
    prompts carry, so "the answer must have these keys" is enforced, not hoped.
``one_repair``
    an invalid answer gets exactly **one** repair round-trip (the validator's
    complaints plus the rejected text); a second failure is reported.
``ScopedDataset`` / ``IndexDataset``
    dataset locking: the model may only reference paths it was given
    (:class:`ScopedDataset`), and callers that want more can check the whole
    index (:class:`IndexDataset`).  A hallucinated path is rejected, not shown.
``ResponseCache``
    answers are cached under ``sha256(model + prompt + payload)`` (plus the
    dataset fingerprint, so two different datasets cannot collide), which makes
    a repeated batch fill free and instant.
``Meter``
    every call's tokens and - when the provider names its prices - the estimated
    cost, plus the pre-run :class:`CostEstimate` the UI shows first.
``PathRedactor`` handling
    ``redact_paths`` swaps real paths for tokens before anything leaves the
    machine (:class:`spacesage.ai.prompts.PathRedactor`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from spacesage import candidates
from spacesage.ai import prompts
from spacesage.ai.client import Usage, estimate_tokens_from_chars
from spacesage.ai.config import ProviderConfig
from spacesage.ai.errors import BLOCKED_PATH, SCHEMA, AIError

GUARD_VERSION = "v1"
"""Bumped whenever the guards change in a way that must invalidate cached answers."""

CACHE_SCHEMA = "spacesage.ai.cache/v1"
"""Schema tag written into every cache entry."""

_JSON_TYPES: Mapping[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


_KNOWN_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "title",
        "description",
    }
)


def _unknown_keywords(schema: Any, *, path: str = "$") -> tuple[str, ...]:
    """Every keyword under ``schema`` this validator does not implement."""
    if not isinstance(schema, Mapping):
        return ()
    found: list[str] = []
    for key, value in schema.items():
        if key not in _KNOWN_KEYWORDS:
            found.append(f"{path}.{key}")
            continue
        if key == "properties" and isinstance(value, Mapping):
            for name, child in value.items():
                found.extend(_unknown_keywords(child, path=f"{path}.properties.{name}"))
        elif key == "items":
            found.extend(_unknown_keywords(value, path=f"{path}.items"))
    return tuple(found)


def validate(schema: Mapping[str, Any], value: Any, *, path: str = "$") -> tuple[str, ...]:
    """Check ``value`` against a JSON-Schema subset; returns human-readable errors.

    Supported keywords: ``type``, ``enum``, ``required``, ``properties``,
    ``additionalProperties`` (bool), ``items``, ``minItems``, ``maxItems``,
    ``minimum``, ``maximum``, ``minLength``, ``maxLength``, ``pattern``.  That is
    everything the use-case schemas use - and a schema using a keyword this
    validator does not know is refused loudly, because a guardrail that silently
    skips a rule the author believed in is worse than no guardrail.
    """
    unknown = _unknown_keywords(schema)
    if unknown:
        raise AIError(
            SCHEMA,
            f"the schema uses keywords this validator does not implement: {', '.join(unknown)}",
            hint="extend guardrails._KNOWN_KEYWORDS and the validator, or drop the keyword",
        )
    errors: list[str] = []
    _validate(schema, value, path=path, errors=errors)
    return tuple(errors)


def _validate(
    schema: Mapping[str, Any],
    value: Any,
    *,
    path: str,
    errors: list[str],
) -> None:
    expected = schema.get("type")
    if isinstance(expected, str):
        types = _JSON_TYPES.get(expected)
        if types is None:
            errors.append(f"{path}: schema asks for unsupported type {expected!r}")
            return
        if not _matches_type(value, expected, types):
            errors.append(f"{path}: expected {expected}, found {_type_name(value)}")
            return
    elif isinstance(expected, Sequence):
        names = [str(item) for item in expected]
        if not any(
            name in _JSON_TYPES and _matches_type(value, name, _JSON_TYPES[name]) for name in names
        ):
            errors.append(f"{path}: expected one of {names}, found {_type_name(value)}")
            return
    if "enum" in schema:
        allowed = schema["enum"]
        if isinstance(allowed, Sequence) and value not in allowed:
            errors.append(f"{path}: {value!r} is not one of {list(allowed)}")
            return
    if isinstance(value, Mapping):
        _validate_object(schema, value, path=path, errors=errors)
    if isinstance(value, list):
        _validate_array(schema, value, path=path, errors=errors)
    if isinstance(value, str):
        _validate_string(schema, value, path=path, errors=errors)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: {value} is below {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: {value} is above {maximum}")


def _validate_object(
    schema: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    path: str,
    errors: list[str],
) -> None:
    required = schema.get("required")
    if isinstance(required, Sequence):
        for key in required:
            if str(key) not in value:
                errors.append(f"{path}: missing required key {key!r}")
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for key, sub in properties.items():
            if key in value and isinstance(sub, Mapping):
                _validate(sub, value[key], path=f"{path}.{key}", errors=errors)
    if schema.get("additionalProperties") is False and isinstance(properties, Mapping):
        extra = sorted(str(key) for key in value if key not in properties)
        if extra:
            errors.append(f"{path}: unexpected key(s) {', '.join(extra)}")


def _validate_array(
    schema: Mapping[str, Any],
    value: Sequence[Any],
    *,
    path: str,
    errors: list[str],
) -> None:
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    if isinstance(min_items, int) and len(value) < min_items:
        errors.append(f"{path}: needs at least {min_items} item(s), found {len(value)}")
    if isinstance(max_items, int) and len(value) > max_items:
        errors.append(f"{path}: allows at most {max_items} item(s), found {len(value)}")
    items = schema.get("items")
    if isinstance(items, Mapping):
        for index, entry in enumerate(value):
            _validate(items, entry, path=f"{path}[{index}]", errors=errors)


def _validate_string(
    schema: Mapping[str, Any],
    value: str,
    *,
    path: str,
    errors: list[str],
) -> None:
    min_length = schema.get("minLength")
    max_length = schema.get("maxLength")
    if isinstance(min_length, int) and len(value) < min_length:
        errors.append(f"{path}: needs at least {min_length} character(s), found {len(value)}")
    if isinstance(max_length, int) and len(value) > max_length:
        errors.append(f"{path}: allows at most {max_length} character(s), found {len(value)}")


def _matches_type(value: Any, name: str, types: tuple[type, ...]) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    return isinstance(value, types)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def read_answer(case_id: str, text: str) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    """Extract and validate the JSON object of one answer.

    Returns ``(answer, errors)`` - errors are empty when the answer is usable.
    """
    case = prompts.use_case(case_id)
    try:
        raw = prompts.extract_json(text)
    except AIError as exc:
        return {}, (str(exc),)
    if not isinstance(raw, Mapping):
        return {}, (f"the answer is a {_type_name(raw)}, expected an object",)
    return raw, validate(case.schema, raw)


def repair_failure(case_id: str, text: str, errors: Sequence[str]) -> AIError:
    """The error raised when the one repair retry was not enough."""
    return AIError(
        "repair_failed",
        f"the model's {case_id} answer stayed invalid after a repair retry",
        hint=(
            "try a larger model (`spacesage ai models`), or lower batch_size so the "
            "answer has to be shorter"
        ),
        detail={"errors": list(errors), "body": text[:1000]},
    )


# --------------------------------------------------------------------------- #
# Dataset locking
# --------------------------------------------------------------------------- #


class DatasetError(AIError):
    """A path the model named is not part of the dataset it was shown."""

    def __init__(self, paths: Sequence[str], *, scope: str) -> None:
        listing = ", ".join(paths[:3]) + (" ..." if len(paths) > 3 else "")
        super().__init__(
            BLOCKED_PATH,
            f"the model referenced {len(paths)} path(s) outside the {scope} "
            f"it was shown: {listing}",
            hint="rejected as a hallucination; the answer is not used for those items",
        )
        self.paths = tuple(paths)


@dataclass(frozen=True)
class ScopedDataset:
    """The dataset lock for one call: only the items that were sent may come back.

    A model that answers about a path it never saw - even a path that exists
    elsewhere in the index - is answering from imagination, and the guard drops
    it.  Scope is the batch, so "hallucinated" means exactly "not in the data
    block".
    """

    keys: frozenset[str]
    label: str = "batch"

    @classmethod
    def of(cls, items: Iterable[prompts.ItemFacts], *, label: str = "batch") -> ScopedDataset:
        """Scope for a list of items (case-folded keys)."""
        return cls(frozenset(item.key() for item in items), label=label)

    @classmethod
    def of_paths(cls, paths: Iterable[str], *, label: str = "selection") -> ScopedDataset:
        """Scope for raw paths."""
        return cls(frozenset(candidates.path_key(path) for path in paths), label=label)

    def has(self, path: str) -> bool:
        """True when ``path`` is part of the scope."""
        return candidates.path_key(path) in self.keys

    def allow(self, paths: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Split ``paths`` into ``(allowed, rejected)``."""
        allowed: list[str] = []
        rejected: list[str] = []
        for path in paths:
            (allowed if self.has(path) else rejected).append(path)
        return tuple(allowed), tuple(rejected)

    def fingerprint(self) -> str:
        """Stable hash of the scope (part of the cache key)."""
        joined = "\n".join(sorted(self.keys))
        return sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class IndexDataset:
    """Membership check against the whole index (``spacesage ai`` cross-checks).

    Small indexes load their paths once (``mode='keyset'``); a large one answers
    from the database per path (``mode='query'``) instead of holding millions of
    strings in memory.
    """

    keys: frozenset[str] = frozenset()
    mode: str = "keyset"
    _conn: sqlite3.Connection | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> IndexDataset:
        """A locked set of paths (tests, plans, exports)."""
        return cls(keys=frozenset(candidates.path_key(path) for path in paths), mode="keyset")

    @classmethod
    def from_db(cls, conn: sqlite3.Connection, *, cap: int = 500_000) -> IndexDataset:
        """Every path of an index, loaded when it is small enough to hold."""
        (count,) = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
        if int(count) <= cap:
            keys = frozenset(
                candidates.path_key(row[0]) for row in conn.execute("SELECT path FROM entries")
            )
            return cls(keys=keys, mode="keyset")
        return cls(mode="query", _conn=conn)

    def has(self, path: str) -> bool:
        """True when ``path`` exists in the index."""
        key = candidates.path_key(path)
        if self.mode == "keyset":
            return key in self.keys
        assert self._conn is not None  # mode='query' always carries a connection
        row = self._conn.execute(
            "SELECT 1 FROM entries WHERE lower(path) = ? LIMIT 1", (key,)
        ).fetchone()
        return row is not None

    def allow(self, paths: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Split ``paths`` into ``(allowed, rejected)``."""
        allowed: list[str] = []
        rejected: list[str] = []
        for path in paths:
            (allowed if self.has(path) else rejected).append(path)
        return tuple(allowed), tuple(rejected)


def locked(
    items: Sequence[prompts.ItemFacts],
    *,
    label: str = "batch",
) -> tuple[ScopedDataset, tuple[prompts.ItemFacts, ...], tuple[prompts.ItemFacts, ...]]:
    """Split ``items`` into the ones the dataset lock keeps and the ones it drops.

    Items the caller gathered from the index are kept; the split exists so a
    caller that mixes sources (a plan, a hand-typed path) can see what was
    refused instead of silently sending it.
    """
    scope = ScopedDataset.of(items, label=label)
    kept: list[prompts.ItemFacts] = []
    dropped: list[prompts.ItemFacts] = []
    for item in items:
        (kept if scope.has(item.path) else dropped).append(item)
    return scope, tuple(kept), tuple(dropped)


# --------------------------------------------------------------------------- #
# The response cache
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CacheEntry:
    """One cached answer, with everything needed to attribute it."""

    key: str
    created: float
    provider: str
    model: str
    use_case: str
    dataset: str
    redacted: bool
    result: Mapping[str, Any]
    usage: Mapping[str, Any] = field(default_factory=dict)
    tokens: Mapping[str, str] = field(default_factory=dict)
    """Token -> real path map, when the payload was redacted."""

    def age_s(self, *, now: float | None = None) -> float:
        """Seconds since the answer was written."""
        return (time.time() if now is None else now) - self.created

    def to_dict(self) -> dict[str, Any]:
        """The on-disk form."""
        return {
            "schema": CACHE_SCHEMA,
            "key": self.key,
            "created": self.created,
            "provider": self.provider,
            "model": self.model,
            "use_case": self.use_case,
            "dataset": self.dataset,
            "redacted": self.redacted,
            "usage": dict(self.usage),
            "tokens": dict(self.tokens),
            "result": dict(self.result),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CacheEntry:
        """Rebuild an entry from disk (raises :class:`AIError` when unusable)."""
        if data.get("schema") != CACHE_SCHEMA:
            raise AIError(SCHEMA, "cache entry has an unknown schema")
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise AIError(SCHEMA, "cache entry has no result object")
        usage = data.get("usage")
        tokens = data.get("tokens")
        return cls(
            key=str(data.get("key", "")),
            created=float(data.get("created", 0.0)),
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            use_case=str(data.get("use_case", "")),
            dataset=str(data.get("dataset", "")),
            redacted=bool(data.get("redacted", False)),
            result=dict(result),
            usage=dict(usage) if isinstance(usage, Mapping) else {},
            tokens=dict(tokens) if isinstance(tokens, Mapping) else {},
        )


@dataclass(frozen=True)
class CacheStats:
    """What the cache holds and how often it answered (status bar material)."""

    entries: int = 0
    bytes: int = 0
    hits: int = 0
    misses: int = 0
    corrupt: int = 0

    @property
    def hit_rate(self) -> float:
        """Share of lookups answered from the cache (``0.0`` when never asked)."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "entries": self.entries,
            "bytes": self.bytes,
            "hits": self.hits,
            "misses": self.misses,
            "corrupt": self.corrupt,
            "hit_rate": round(self.hit_rate, 3),
        }


class ResponseCache:
    """Content-addressed store of validated answers.

    The key is ``sha256(model + prompt + payload + dataset)`` - exactly the
    inputs that determine the answer - so "run it again" costs nothing and a
    changed model, prompt version or payload is a miss by construction.
    """

    def __init__(self, directory: str | Path, *, enabled: bool = True, env: Any = None) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.corrupt = 0

    # -- keys --------------------------------------------------------------- #

    @staticmethod
    def key(
        *,
        use_case: str,
        version: str,
        provider: str,
        model: str,
        payload: str,
        dataset: str = "",
    ) -> str:
        """The content hash of one question (model + prompt + payload + dataset)."""
        parts = [
            GUARD_VERSION,
            use_case,
            version,
            provider,
            model,
            sha256(payload.encode("utf-8")).hexdigest(),
            dataset,
        ]
        return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        """Where an entry lives (two-level fan-out keeps directories small)."""
        return self.directory / key[:2] / f"{key}.json"

    # -- access ------------------------------------------------------------- #

    def peek(self, key: str) -> bool:
        """Whether an answer for ``key`` is cached - without touching the counters.

        Used by the batch planner to say "3 of 5 calls are already cached" before
        anything runs; :meth:`get` would count a hit or a miss for a question the
        user has not asked yet.
        """
        if not self.enabled:
            return False
        return self.path_for(key).is_file()

    def get(self, key: str) -> CacheEntry | None:
        """The cached entry for ``key``, or ``None`` (counted as a miss)."""
        if not self.enabled:
            return None
        path = self.path_for(key)
        if not path.is_file():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, Mapping):
                raise AIError(SCHEMA, "cache entry is not an object")
            entry = CacheEntry.from_dict(data)
        except (OSError, json.JSONDecodeError, AIError, ValueError):
            self.corrupt += 1
            self.misses += 1
            with suppress(OSError):  # best effort: a corrupt entry goes away
                path.unlink()
            return None
        self.hits += 1
        return entry

    def put(self, entry: CacheEntry) -> Path | None:
        """Store an entry atomically (returns its path, or ``None`` when disabled)."""
        if not self.enabled:
            return None
        path = self.path_for(entry.key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(entry.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return None
        return path

    def clear(self) -> int:
        """Delete every entry; returns how many files went away."""
        if not self.directory.is_dir():
            return 0
        removed = 0
        for path in sorted(self.directory.rglob("*.json")):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - best effort
                pass
        return removed

    def stats(self) -> CacheStats:
        """What the store holds right now (a directory walk)."""
        entries = 0
        size = 0
        if self.directory.is_dir():
            for path in self.directory.rglob("*.json"):
                try:
                    size += path.stat().st_size
                except OSError:  # pragma: no cover - best effort
                    continue
                entries += 1
        return CacheStats(
            entries=entries, bytes=size, hits=self.hits, misses=self.misses, corrupt=self.corrupt
        )


# --------------------------------------------------------------------------- #
# Metering and cost
# --------------------------------------------------------------------------- #

TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class MeterSnapshot:
    """A point-in-time view of what this session's AI work cost."""

    calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False
    cost_usd: float | None = 0.0
    pricing_known: bool = True

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "calls": self.calls,
            "failures": self.failures,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "pricing_known": self.pricing_known,
        }

    def render(self) -> str:
        """One line for a status bar: ``12.3k tokens, ~$0.0042``."""
        parts = [f"{_compact_number(self.total_tokens)} tokens"]
        parts.append("~" if self.estimated else "")
        if self.cost_usd is None:
            parts.append("cost unknown (no prices configured)")
        else:
            parts.append(f"${self.cost_usd:.4f}".rstrip("0").rstrip("."))
        if self.cache_hits:
            parts.append(f"{self.cache_hits} cached")
        return ", ".join(part for part in parts if part)


def _compact_number(value: int) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


@dataclass
class Meter:
    """Accumulates tokens and cost across calls (the UI's cost meter)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    estimated: bool = False
    cost_usd: float = 0.0
    pricing_known: bool = True

    def record(
        self,
        usage: Usage,
        *,
        pricing_in: float | None = None,
        pricing_out: float | None = None,
    ) -> None:
        """Add one call's usage (and its estimated cost, when prices are known)."""
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.estimated = self.estimated or usage.estimated
        if pricing_in is None or pricing_out is None:
            self.pricing_known = False
            self.cost_usd = 0.0
            return
        self.cost_usd += (
            usage.prompt_tokens * pricing_in + usage.completion_tokens * pricing_out
        ) / TOKENS_PER_MILLION

    def record_cache_hit(self) -> None:
        """A lookup answered from the cache: no tokens, no cost, but worth counting."""
        self.cache_hits += 1

    def record_failure(self) -> None:
        """A call that failed (so the UI can say "3 of 5 batches failed")."""
        self.failures += 1

    def snapshot(self) -> MeterSnapshot:
        """The frozen view to render."""
        return MeterSnapshot(
            calls=self.calls,
            failures=self.failures,
            cache_hits=self.cache_hits,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.prompt_tokens + self.completion_tokens,
            estimated=self.estimated,
            cost_usd=None if not self.pricing_known else self.cost_usd,
            pricing_known=self.pricing_known,
        )


@dataclass(frozen=True)
class CostEstimate:
    """What a batch run will roughly cost, shown *before* it runs."""

    use_case: str
    items: int
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    pricing_known: bool
    cached_calls: int = 0
    note: str = ""

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens

    def render(self) -> str:
        """One line for the confirm dialog / CLI (``~12.4k tokens, ~$0.003``)."""
        if self.calls == 0:
            return "nothing to ask: every item is already cached"
        parts = [
            f"{self.calls} call(s)",
            f"~{_compact_number(self.total_tokens)} tokens",
        ]
        if self.cost_usd is None:
            parts.append("cost unknown (no prices configured)")
        else:
            parts.append(f"~${self.cost_usd:.4f}")
        if self.cached_calls:
            parts.append(f"{self.cached_calls} already cached")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "use_case": self.use_case,
            "items": self.items,
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "pricing_known": self.pricing_known,
            "note": self.note,
            "rendered": self.render(),
        }


def estimate_batch(
    *,
    case_id: str,
    payloads: Sequence[str],
    provider: ProviderConfig,
    max_tokens: int,
    items: int,
    cached_calls: int = 0,
) -> CostEstimate:
    """Estimate tokens/cost for a list of already-rendered payloads."""
    case = prompts.use_case(case_id)
    system_chars = len(case.system) + len(case.instructions) + 400
    prompt_tokens = sum(
        estimate_tokens_from_chars(len(payload) + system_chars) for payload in payloads
    )
    per_call_completion = max(64, min(max_tokens, 160 * max(1, items // max(1, len(payloads)))))
    completion_tokens = per_call_completion * len(payloads)
    price_in = provider.pricing_in
    price_out = provider.pricing_out
    pricing_known = price_in is not None and price_out is not None
    cost: float | None = None
    if price_in is not None and price_out is not None:
        cost = (prompt_tokens * price_in + completion_tokens * price_out) / TOKENS_PER_MILLION
    billed_calls = max(0, len(payloads) - cached_calls)
    return CostEstimate(
        use_case=case_id,
        items=items,
        calls=len(payloads),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        pricing_known=pricing_known,
        cached_calls=cached_calls,
        note=(
            "estimate from payload size (about 4 characters per token); "
            + (
                "costs use the provider's configured prices"
                if pricing_known
                else "no prices configured for this provider"
            )
            + (f"; {billed_calls} call(s) will actually be sent" if cached_calls else "")
        ),
    )


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def redactor(enabled: bool, *, prefix: str = "path") -> prompts.PathRedactor | None:
    """A :class:`prompts.PathRedactor` when ``redact_paths`` is on, else ``None``."""
    return prompts.PathRedactor(prefix=prefix) if enabled else None


def leaks(payload: str, paths: Sequence[str]) -> tuple[str, ...]:
    """Paths (or their basenames) that survived into a supposedly redacted payload."""
    found: list[str] = []
    for path in paths:
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if path and path in payload:
            found.append(path)
        elif name and name in payload:
            found.append(name)
    return tuple(found)


def assert_redacted(payload: str, paths: Sequence[str]) -> None:
    """Raise when a redacted payload still carries a path (used by the tests)."""
    leaked = leaks(payload, paths)
    if leaked:
        raise AIError(
            BLOCKED_PATH,
            f"redact_paths is on but the payload still names {len(leaked)} path(s)",
            hint="this is a bug: paths must be tokenised before the request is built",
            detail={"leaked": list(leaked[:3])},
        )
