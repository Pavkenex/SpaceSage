"""Rule promotion: write an accepted AI verdict into the user's rule file.

The AI's classifications and suggestions are one-click promotions away from
becoming ordinary rule-pack entries (``~/.config/spacesage/rules/ai-promoted.toml``,
``$SPACESAGE_RULES_DIR`` overrides).  That is the whole point of the layer: the
model helps once, the deterministic engine does the work from then on - and the
promotion is a *file write*, reviewed by a human, never an execution.

What the promotion refuses, and why:

* a destructive action for an entry that has no T1/T2 tier (an undecided row is
  T3 by construction) - classify it first, then promote;
* a destructive action below :data:`DEFAULT_CONFIDENCE_FLOOR` - "probably a
  cache" is not good enough to delete on sight;
* a pattern wilder than the item itself: a promoted rule matches *that folder's
  subtree* or *that file*, never ``**/*.tmp``.  Broad rules stay a human edit;
* writing into a file this module does not own (its ``[pack] id`` is different)
  - the user's packs are never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from spacesage import rules
from spacesage.ai.errors import INVALID_CONFIG, AIError
from spacesage.ai.prompts import Classification, ItemFacts, Suggestion

PACK_ID = "ai-promoted"
"""Pack id of the file promotions are written to."""

PACK_TITLE = "Promoted from AI suggestions"
"""Pack title shown by ``spacesage classify --list-rules``."""

PACK_ORDER = 5
"""User packs default to order 5; promoted rules keep that slot."""

DEFAULT_CONFIDENCE_FLOOR = 0.8
"""A destructive action needs at least this much confidence to be promotable."""

DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({"DELETE_QUARANTINE", "COMPRESS_NTFS"})
"""Promoting one of these needs a T1/T2 tier and the confidence floor."""

_RULE_ID_OK = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PromotionError(AIError):
    """The verdict may not become a rule (with the reason in the message)."""

    def __init__(
        self, message: str, *, hint: str = "", detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(INVALID_CONFIG, message, hint=hint, detail=detail)


@dataclass(frozen=True)
class RuleDraft:
    """One rule-pack entry, ready to be reviewed as TOML and written to disk."""

    id: str
    category: str
    tier: str
    action: str
    confidence: float
    rationale: str
    paths: tuple[str, ...] = ()
    exts: tuple[str, ...] = ()
    min_size: int | None = None
    older_than_days: float | None = None
    name_regex: str | None = None
    native: str | None = None
    note: str = ""
    """Where the entry came from (shown in the preview, never written)."""

    def validate(self) -> None:
        """Refuse a draft the engine could not load (fail before writing)."""
        if not _RULE_ID_OK.match(self.id):
            raise PromotionError(
                f"rule id {self.id!r} is not a valid rule id",
                hint="ids are lowercase and may contain dots, dashes and underscores",
            )
        if self.tier not in rules.TIERS:
            raise PromotionError(
                f"unknown tier {self.tier!r}", hint=f"tiers: {', '.join(rules.TIERS)}"
            )
        if self.action not in rules.ACTIONS:
            raise PromotionError(
                f"action {self.action!r} is not in the engine's vocabulary",
                hint=f"actions: {', '.join(rules.ACTIONS)}",
            )
        if not self.paths and not self.exts and self.name_regex is None:
            raise PromotionError(
                f"rule {self.id!r} has no matcher",
                hint="a rule without a path, extension or name matcher would match everything",
            )
        if not self.rationale.strip():
            raise PromotionError(f"rule {self.id!r} needs a rationale")
        if self.name_regex is not None:
            try:
                re.compile(self.name_regex)
            except re.error as exc:
                raise PromotionError(
                    f"rule {self.id!r} has an invalid name_regex: {exc}",
                    hint="drop the regex, or fix it before promoting",
                ) from exc

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (the preview dialog's model)."""
        return {
            "id": self.id,
            "category": self.category,
            "tier": self.tier,
            "action": self.action,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "path": list(self.paths),
            "ext": list(self.exts),
            "min_size": self.min_size,
            "older_than_days": self.older_than_days,
            "name_regex": self.name_regex,
            "native": self.native,
            "note": self.note,
        }

    def render(self) -> str:
        """The ``[[rule]]`` block written to the pack."""
        self.validate()
        lines = ["[[rule]]", f"id = {_toml_string(self.id)}"]
        if self.paths:
            lines.append(f"path = [{', '.join(_toml_string(path) for path in self.paths)}]")
        if self.exts:
            lines.append(f"ext = [{', '.join(_toml_string(ext) for ext in self.exts)}]")
        if self.min_size is not None:
            lines.append(f"min_size = {int(self.min_size)}")
        if self.older_than_days is not None:
            lines.append(f"older_than_days = {_toml_number(self.older_than_days)}")
        if self.name_regex is not None:
            lines.append(f"name_regex = {_toml_string(self.name_regex)}")
        lines.append(f"category = {_toml_string(self.category)}")
        lines.append(f"tier = {_toml_string(self.tier)}")
        lines.append(f"action = {_toml_string(self.action)}")
        lines.append(f"confidence = {_toml_number(self.confidence)}")
        lines.append(f"rationale = {_toml_string(self.rationale)}")
        if self.native:
            lines.append(f"native = {_toml_string(self.native)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class PromotionResult:
    """What a promotion did (or would do, with ``dry_run``)."""

    ok: bool
    path: Path | None = None
    written: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    dry_run: bool = False
    toml: str = ""
    fingerprint: str = ""
    error: AIError | None = None

    def render(self) -> str:
        """One line for a toast or the CLI."""
        if not self.ok:
            return f"rule not written: {self.error}"
        action = "would write" if self.dry_run else "wrote"
        parts = [f"{action} {len(self.written) + len(self.replaced)} rule(s) to {self.path}"]
        if self.replaced:
            parts.append(f"replaced {', '.join(self.replaced)}")
        return "; ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "ok": self.ok,
            "path": None if self.path is None else str(self.path),
            "written": list(self.written),
            "replaced": list(self.replaced),
            "dry_run": self.dry_run,
            "fingerprint": self.fingerprint,
            "error": None if self.error is None else self.error.to_dict(),
            "rendered": self.render(),
        }


# --------------------------------------------------------------------------- #
# Drafts from AI verdicts
# --------------------------------------------------------------------------- #


def draft_from_suggestion(
    item: ItemFacts,
    suggestion: Suggestion,
    *,
    tier: str | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    rule_id: str | None = None,
) -> RuleDraft:
    """Turn an accepted suggestion into a minimal, path-anchored rule draft.

    The tier can only come from the item's existing classification (or the
    caller): ``suggest`` does not assign tiers, so a destructive suggestion for
    an undecided entry is refused rather than promoted at an invented tier.
    """
    return _draft(
        item=item,
        action=suggestion.engine_action,
        category=_category_for(item, suggestion.action),
        tier=tier or item.tier or "T3",
        confidence=suggestion.confidence,
        rationale=_rationale(suggestion.why, suggestion.label),
        native=suggestion.native,
        note=f"AI suggestion: {suggestion.label} ({suggestion.confidence:.0%} confident)",
        confidence_floor=confidence_floor,
        rule_id=rule_id,
    )


def draft_from_classification(
    item: ItemFacts,
    classification: Classification,
    *,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    rule_id: str | None = None,
) -> RuleDraft:
    """Turn an accepted classification into a rule draft (tier comes with it)."""
    return _draft(
        item=item,
        action=classification.action,
        category=classification.category,
        tier=classification.tier,
        confidence=classification.confidence,
        rationale=classification.rationale,
        native=classification.native,
        name_regex=classification.name_regex,
        note=(
            f"AI classification: {classification.category} "
            f"({classification.confidence:.0%} confident)"
        ),
        confidence_floor=confidence_floor,
        rule_id=rule_id,
    )


def _draft(
    *,
    item: ItemFacts,
    action: str,
    category: str,
    tier: str,
    confidence: float,
    rationale: str,
    native: str | None,
    note: str,
    confidence_floor: float,
    rule_id: str | None,
    name_regex: str | None = None,
) -> RuleDraft:
    if tier not in rules.TIERS:
        raise PromotionError(
            f"cannot promote at tier {tier!r}",
            hint=f"tiers: {', '.join(rules.TIERS)}",
        )
    if action in DESTRUCTIVE_ACTIONS:
        if tier not in {"T1", "T2"}:
            raise PromotionError(
                f"refusing a {action} rule for {item.path}: the entry has no T1/T2 tier yet",
                hint=(
                    "run the AI classification for this entry first (classify assigns a tier), "
                    "or promote a REVIEW rule instead"
                ),
            )
        if confidence < confidence_floor:
            raise PromotionError(
                f"refusing a {action} rule for {item.path}: confidence {confidence:.2f} "
                f"is below {confidence_floor:.2f}",
                hint="answer the entry yourself, or promote a REVIEW rule and handle it by hand",
            )
    draft = RuleDraft(
        id=rule_id or rule_id_for(item.path),
        category=_slug(category) or "ai-classified",
        tier=tier,
        action=action,
        confidence=round(float(confidence), 2),
        rationale=rationale.strip()[:400],
        paths=(pattern_for(item),),
        exts=(),
        min_size=None,
        older_than_days=None,
        name_regex=name_regex,
        native=native,
        note=note,
    )
    draft.validate()
    return draft


def pattern_for(item: ItemFacts) -> str:
    """The narrowest pattern that covers the item: the folder subtree, or the file."""
    if item.is_dir:
        return item.path.rstrip("\\/") + "/**"
    return item.path


def rule_id_for(path: str) -> str:
    """A stable, readable rule id for a path (``promote-cache-1a2b3c4d``)."""
    name = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "root"
    slug = _slug(name) or "path"
    digest = sha256(path.encode("utf-8")).hexdigest()[:8]
    candidate = f"promote-{slug}-{digest}"
    return candidate[:64]


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    return cleaned[:48]


def _category_for(item: ItemFacts, action: str) -> str:
    if item.category and item.category != rules.UNKNOWN_CATEGORY:
        return item.category
    return {
        "DELETE_QUARANTINE": "ai-promoted-cleanup",
        "MOVE": "ai-promoted-move",
        "COMPRESS": "ai-promoted-compress",
        "NATIVE": "ai-promoted-native",
        "LINK": "ai-promoted-duplicates",
        "REVIEW": "ai-promoted-review",
        "NO_ACTION": "ai-promoted-keep",
    }.get(action, "ai-promoted")


def _rationale(why: str, label: str) -> str:
    text = why.strip()
    if not text:
        text = f"{label}: no reason given by the model."
    if not text.lower().startswith(label.lower()):
        text = f"{label}: {text}"
    return text


def draft_from_rule(rule: rules.Rule) -> RuleDraft:
    """A draft that reproduces an already-written rule (used when merging)."""
    return RuleDraft(
        id=rule.id,
        category=rule.category,
        tier=rule.tier,
        action=rule.action,
        confidence=rule.confidence,
        rationale=rule.rationale,
        paths=tuple(rule.paths),
        exts=tuple(sorted(rule.exts)),
        min_size=rule.min_size,
        older_than_days=rule.older_than_days,
        name_regex=rule.name_pattern,
        native=rule.native,
        note=rule.pack,
    )


# --------------------------------------------------------------------------- #
# Preview and write
# --------------------------------------------------------------------------- #


def preview(drafts: Sequence[RuleDraft], *, pack_id: str = PACK_ID) -> str:
    """The exact file text a write would produce (the confirmation dialog's body)."""
    return render_pack(drafts, pack_id=pack_id)


def render_pack(
    drafts: Sequence[RuleDraft],
    *,
    pack_id: str = PACK_ID,
    title: str = PACK_TITLE,
    order: int = PACK_ORDER,
) -> str:
    """Render a complete rule-pack file: header, ``[pack]`` table, one block per rule."""
    lines = [
        "# SpaceSage user rules promoted from accepted AI verdicts (docs/ai.md).",
        "# Managed by the app: hand edits are kept as long as the TOML stays valid,",
        "# but the app rewrites this file when a new verdict is promoted.",
        "",
        "[pack]",
        f"id = {_toml_string(pack_id)}",
        f"title = {_toml_string(title)}",
        f"order = {order}",
    ]
    for draft in drafts:
        lines.append("")
        lines.append(draft.render())
    return "\n".join(lines).rstrip() + "\n"


def merge_drafts(
    existing: Sequence[RuleDraft], incoming: Iterable[RuleDraft]
) -> tuple[RuleDraft, ...]:
    """Add new drafts, replace same-id ones in place, keep the order stable."""
    merged = list(existing)
    index = {draft.id: position for position, draft in enumerate(merged)}
    for draft in incoming:
        position = index.get(draft.id)
        if position is None:
            index[draft.id] = len(merged)
            merged.append(draft)
        else:
            merged[position] = draft
    return tuple(merged)


def write_rules(
    drafts: Sequence[RuleDraft],
    *,
    dest_dir: str | Path | None = None,
    pack_id: str = PACK_ID,
    dry_run: bool = False,
) -> PromotionResult:
    """Write promoted rules into the user pack (atomically, validated first).

    The file is rendered, parsed back with the engine's own loader, and only then
    moved into place - a broken pack would break every later classify run, so a
    validation failure means nothing is written at all.
    """
    for draft in drafts:
        try:
            draft.validate()
        except PromotionError as exc:
            return PromotionResult(ok=False, error=exc, dry_run=dry_run)
    directory = Path(dest_dir) if dest_dir is not None else rules.default_rules_dir()
    path = directory / f"{pack_id}.toml"
    existing: tuple[RuleDraft, ...] = ()
    if path.is_file():
        try:
            existing = _existing_drafts(path, pack_id)
        except PromotionError as exc:
            return PromotionResult(ok=False, error=exc, dry_run=dry_run, path=path)
    merged = merge_drafts(existing, drafts)
    text = render_pack(merged, pack_id=pack_id)
    result = PromotionResult(
        ok=True,
        path=path,
        written=tuple(
            draft.id for draft in drafts if draft.id not in {item.id for item in existing}
        ),
        replaced=tuple(draft.id for draft in drafts if draft.id in {item.id for item in existing}),
        dry_run=dry_run,
        toml=text,
        fingerprint=sha256(text.encode("utf-8")).hexdigest()[:16],
    )
    if dry_run:
        return result
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{pack_id}.toml.tmp"
        tmp.write_text(text, encoding="utf-8")
        _check_written_pack(tmp)  # the engine's own loader, before anything is replaced
        os.replace(tmp, path)
    except OSError as exc:
        return PromotionResult(
            ok=False,
            error=PromotionError(
                f"cannot write {path}: {exc}",
                hint="check that the rules directory is writable",
            ),
            dry_run=dry_run,
            path=path,
        )
    except PromotionError as exc:
        with suppress(OSError):  # best effort: the temp file never exists on a win
            (directory / f".{pack_id}.toml.tmp").unlink()
        return PromotionResult(ok=False, error=exc, dry_run=dry_run, path=path)
    return result


def _existing_drafts(path: Path, pack_id: str) -> tuple[RuleDraft, ...]:
    """Read the pack this module owns, refusing anything else."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PromotionError(
            f"{path} is not valid TOML ({exc})",
            hint="fix or delete the file; nothing was written",
        ) from exc
    pack = data.get("pack")
    if isinstance(pack, Mapping) and pack.get("id") not in {None, pack_id}:
        raise PromotionError(
            f"{path} belongs to rule pack {pack.get('id')!r}, not {pack_id!r}",
            hint="promote into a different --rule-dir, or rename the file",
        )
    try:
        loaded = rules.load_pack(path, user=True)
    except rules.RulesError as exc:
        raise PromotionError(
            f"{path} does not load: {exc}",
            hint="fix the file first; nothing was written",
        ) from exc
    return tuple(draft_from_rule(rule) for rule in loaded.rules)


def _check_written_pack(tmp: Path) -> None:
    """Parse the freshly rendered file with the engine's loader."""
    try:
        rules.load_pack(tmp, user=True)
    except rules.RulesError as exc:
        raise PromotionError(
            f"the promoted pack does not load: {exc}",
            hint="this is a bug - report it with the rule that produced it",
        ) from exc


def _toml_string(value: str) -> str:
    """A TOML basic string (JSON escaping is a valid subset for our values)."""
    return json.dumps(value, ensure_ascii=False)


def _toml_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


__all__ = [
    "DEFAULT_CONFIDENCE_FLOOR",
    "DESTRUCTIVE_ACTIONS",
    "PACK_ID",
    "PromotionError",
    "PromotionResult",
    "RuleDraft",
    "draft_from_classification",
    "draft_from_rule",
    "draft_from_suggestion",
    "merge_drafts",
    "pattern_for",
    "preview",
    "render_pack",
    "rule_id_for",
    "write_rules",
]
