"""Live-tree fixtures for the executor tests (and a demo tree).

The executor is the one stage that *does* things: quarantine, move, link, undo.
Its tests therefore need a real tree whose bytes they can compare afterwards,
not a CSV.  This module plants that tree deterministically -- fixed sizes, fixed
content, fixed layout, so every digest in a test is arithmetic -- and provides
the helpers the tests lean on:

``plant_tree(root)``
    the standard tree: ``media/`` (a folder to move), ``cache/`` (a folder to
    quarantine), ``keep/`` (never touched) and an empty ``target/``.
``snapshot(root)``
    ``{relative path: "DIR" | "LINK:<target>" | sha256}`` -- the byte-identity
    check every round-trip test uses.
``plan_dict(...)`` / ``action(...)``
    build a *valid* ``plan.json`` v1 (same ``plan_id`` recipe the engine uses)
    for any set of actions, so tests never hand-write the contract.
``scenario(tmp)``
    the standard tree + plan + ``approved.json``, ready to apply.

``python tests/fixtures/gen_executor.py <dir>`` plants the demo tree and writes
the plan next to it (the artefact the slice's demo run uses).
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spacesage import executor, planner

DEFAULT_SOURCE: Mapping[str, str] = {
    "csv": "tests/fixtures/data/executor.csv",
    "machine": "FIXTURE-PC",
    "exported": "2026-09-12T12:00:00+00:00",
}
"""Pinned source identity so a fixture plan id is stable on every machine."""

CREATED = "2026-09-12T12:00:00+00:00"
"""Fixed ``created`` (volatile, deliberately outside the plan id)."""

MIB = 1024**2
KIB = 1024

#: The standard tree: ``size`` bytes per file, content derived from the seed.
TREE: Mapping[str, int] = {
    "media/movie.mp4": 2 * MIB,
    "media/clips/clip.mkv": MIB // 2,
    "cache/blob.bin": 256 * KIB,
    "cache/deep/nested.bin": 64 * KIB,
    "keep/notes.txt": KIB,
}


@dataclass(frozen=True)
class Scenario:
    """The standard tree, its plan and the approval that goes with it."""

    tmp: Path
    root: Path
    """The tree being analysed (``tmp/tree``)."""

    target: Path
    """The 'target drive' moves go to (``tmp/target``)."""

    quarantine: Path
    """The quarantine store root the tests use (``tmp/quarantine``)."""

    plan: Mapping[str, object]
    plan_path: Path
    manifest_path: Path
    journal_path: Path

    @property
    def plan_id(self) -> str:
        return str(self.plan["plan_id"])

    def manifest(self, *ids: str) -> executor.Manifest:
        """An approval for the given ids (default: every action of the plan)."""
        selected = ids or tuple(str(entry["id"]) for entry in _actions_of(self.plan))
        return executor.make_manifest(self.plan_id, list(selected), created=CREATED)

    def write_manifest(self, *ids: str) -> Path:
        """Write ``approved.json`` for the given ids and return its path."""
        return executor.write_manifest(self.manifest_path, self.manifest(*ids))


def content(seed: str, size: int) -> bytes:
    """Deterministic bytes: ``seed`` repeated up to ``size``."""
    pattern = f"--{seed}--".encode()
    repeats = size // len(pattern) + 1
    return (pattern * repeats)[:size]


def write_file(path: Path, size: int, *, seed: str | None = None) -> Path:
    """Create ``path`` (and its parents) with ``size`` deterministic bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content(seed or path.name, size))
    return path


def plant_tree(root: Path, *, extra: Mapping[str, int] | None = None) -> Path:
    """Plant the standard tree below ``root`` (plus any ``extra`` files)."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, size in {**TREE, **(extra or {})}.items():
        write_file(root / relative, size)
    return root


def snapshot(root: Path) -> dict[str, str]:
    """``{relative path: sha256 | "DIR" | "LINK:<target>"}`` for a whole tree."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = f"LINK:{path.readlink().as_posix()}"
        elif path.is_dir():
            result[relative] = "DIR"
        else:
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def counts(root: Path) -> tuple[int, int]:
    """``(files, bytes)`` below ``root``, links not followed."""
    files = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            files += 1
            total += path.stat().st_size
    return files, total


def action(
    action_id: str,
    action_type: str,
    path: Path | str,
    size: int,
    *,
    tier: str = "T1",
    kind: str = "delete",
    dest: Path | str | None = None,
    link: str | None = None,
    category: str = "test-category",
    confidence: float = 0.9,
    why: str = "The rule says so.",
    rationale: str = "The rule says so.",
    command: str | None = None,
    weak: bool = False,
) -> planner.PlanAction:
    """One valid ``PlanAction`` (``elevation_required`` follows the link policy)."""
    return planner.PlanAction(
        id=action_id,
        type=action_type,
        kind=kind,
        path=str(path),
        bytes=size,
        category=category,
        tier=tier,
        confidence=confidence,
        rationale=rationale,
        why=why,
        dest=str(dest) if dest is not None else None,
        link_after=link,
        elevation_required=link == "SYMLINK",
        command=command,
        weak=weak,
    )


def plan_dict(
    actions: Sequence[planner.PlanAction],
    *,
    targets: Mapping[str, int] | None = None,
    source: Mapping[str, str] | None = None,
    created: str = CREATED,
) -> dict[str, object]:
    """A complete, valid ``plan.json`` v1 for these actions (real ``plan_id``)."""
    identity = dict(source or DEFAULT_SOURCE)
    return {
        "schema": planner.SCHEMA,
        "plan_id": planner.compute_plan_id(identity, actions),
        "created": created,
        "source": identity,
        "targets": {
            name: {"free_bytes": free, "reserve_bytes": 0} for name, free in (targets or {}).items()
        },
        "provenance": {"index": "fixture"},
        "summary": planner._recompute_summary([item.to_dict() for item in actions])
        | {"dropped": 0},
        "actions": [item.to_dict() for item in actions],
    }


def _actions_of(plan: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """The action mappings of a plan document (already validated by the caller)."""
    raw = plan.get("actions")
    assert isinstance(raw, list)
    return tuple(entry for entry in raw if isinstance(entry, Mapping))


def standard_actions(root: Path, target: Path) -> tuple[planner.PlanAction, ...]:
    """The three actions of the standard plan (before they become a document).

    ``a1`` quarantines ``cache/`` (T1), ``a2`` moves ``media/`` to the target
    drive and links the original path back (T2), ``a3`` is a review item that
    must never execute.
    """
    files, size = counts(root / "cache")
    media_files, media_size = counts(root / "media")
    assert files and media_files
    return (
        action("a1", "DELETE_QUARANTINE", root / "cache", size, kind="delete", tier="T1"),
        action(
            "a2",
            "MOVE",
            root / "media",
            media_size,
            kind="move",
            tier="T2",
            dest=target / "Moved" / "media",
            link="JUNCTION",
        ),
        action("a3", "REVIEW", root / "keep", KIB, kind="stale", tier="T2", confidence=0.4),
    )


def standard_plan(root: Path, target: Path) -> dict[str, object]:
    """The standard plan document for a planted tree."""
    return plan_dict(standard_actions(root, target), targets={str(target): 10**9})


def scenario(
    tmp: Path,
    *,
    ids: Sequence[str] = ("a1", "a2", "a3"),
) -> Scenario:
    """Plant the tree, build the standard plan and write plan + manifest."""
    root = plant_tree(tmp / "tree")
    target = tmp / "target"
    target.mkdir(parents=True, exist_ok=True)
    store = tmp / "quarantine"
    plan = standard_plan(root, target)
    plan_path = tmp / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    manifest_path = tmp / "approved.json"
    journal_path = tmp / "spacesage.journal.jsonl"
    result = Scenario(
        tmp=tmp,
        root=root,
        target=target,
        quarantine=store,
        plan=plan,
        plan_path=plan_path,
        manifest_path=manifest_path,
        journal_path=journal_path,
    )
    result.write_manifest(*ids)
    return result


def apply(
    result: Scenario,
    *ids: str,
    execute: bool = True,
    journal: Path | None = None,
    quarantine_root: Path | None = None,
    backend: object | None = None,
) -> executor.ApplyReport:
    """Apply the approved ids of a scenario (execute by default, journaled)."""
    return executor.apply_plan(
        result.plan,
        result.manifest(*ids),
        execute=execute,
        journal=journal or result.journal_path,
        quarantine_root=quarantine_root or result.quarantine,
        plan_path=result.plan_path,
        manifest_path=result.manifest_path,
        backend=backend,  # type: ignore[arg-type]
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Plant the demo tree in ``argv[0]`` (default: ./demo-tree) with its plan."""
    args = list(argv if argv is not None else sys.argv[1:])
    base = Path(args[0] if args else "demo-tree").resolve()
    tree = plant_tree(base / "tree")
    target = base / "target"
    target.mkdir(parents=True, exist_ok=True)
    plan = standard_plan(tree, target)
    plan_path = base / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    manifest_path = executor.write_manifest(
        base / "approved.json",
        executor.make_manifest(str(plan["plan_id"]), ["a1", "a2"], created=CREATED),
    )
    print(f"planted {tree}")
    print(f"target  {target}")
    print(f"plan    {plan_path} ({len(_actions_of(plan))} actions, {plan['plan_id']})")
    print(f"approve {manifest_path} (a1, a2)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
