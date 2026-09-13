"""[S12] The engine pipeline, end to end, over a planted "full disk".

Every stage runs the way CI and a user run it -- ``python -m spacesage`` in a
subprocess -- and every stage is checked against the *disk itself*, not just
against the previous stage: the export has to describe the planted tree, the
index has to hold what the export says, the ranked candidates have to name
entries that exist with the sizes they claim, the plan has to stay inside the
safety model, and the apply/undo pair has to put the filesystem back exactly as
it was.

The numbers are carried across the stages on purpose: the same bytes are the
ingest total, the candidate totals, ``summary.planned_bytes``, the apply
report's ``reclaimed_bytes`` and the undo report's ``restored_bytes``.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import scenario

from conftest import Cli
from spacesage import executor, planner

MIB = 1024**2

#: The pipeline's thresholds: everything the scenario plants above 1 MiB counts.
MIN_SIZE = "1 MiB"

#: The target drive the plan may move onto (a sandbox: never really free space).
FREE = "50 GiB"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def db_args(planted: scenario.FullDisk, *extra: str) -> tuple[str, ...]:
    """``--db`` for one stage, plus whatever the stage adds (ingest, deepscan)."""
    return ("--db", str(planted.index_dir), *extra)


def stage_args(planted: scenario.FullDisk, *extra: str) -> tuple[str, ...]:
    """``--db``/``--rules`` for one stage: the rule packs are the built-ins only."""
    return ("--db", str(planted.index_dir), "--rules", str(planted.rules_dir), *extra)


def on_disk_bytes(path: Path) -> int:
    """Bytes of a file or folder the way WizTree counts them: files only.

    Folder rows carry the sum of their *files*; the directory entries themselves
    are not part of any total, so neither is a folder's own ``st_size`` here.
    """
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        total += sum((Path(root) / name).lstat().st_size for name in files)
    return total


def covers(names: set[str], path: str) -> bool:
    """True when ``path`` is listed itself or sits inside a listed folder."""
    return any(path == name or path.startswith(name.rstrip("/") + "/") for name in names)


def items_of(report: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    """The listed candidates of one kind block (empty when the kind has none)."""
    for block in report["kinds"]:
        if block["kind"] == kind:
            return list(block["items"])
    raise AssertionError(f"the candidate report has no {kind!r} block")


def blocks(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{kind: block}`` of a candidate report."""
    return {block["kind"]: block for block in report["kinds"]}


def action_paths(plan: Mapping[str, Any]) -> list[str]:
    """Every path the plan names, in action order."""
    return [str(action["path"]) for action in plan["actions"]]


def ingest_stats(cli: Cli, planted: scenario.FullDisk, *extra: str) -> dict[str, int]:
    """Run ``ingest`` and parse its statistics (the engine's own numbers)."""
    result = cli("ingest", str(planted.csv_path), *db_args(planted, *extra))
    stats: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if line.startswith("rows: "):
            head, _, tail = line.partition(" (")
            stats["rows"] = int(head.removeprefix("rows: "))
            files, _, dirs = tail.partition(", ")
            stats["files"] = int(files.removeprefix("files: "))
            stats["dirs"] = int(dirs.removesuffix(")").removeprefix("dirs: "))
        elif line.startswith("total file bytes: "):
            stats["bytes"] = int(line.removeprefix("total file bytes: "))
        elif line.startswith("hardlink file rows: "):
            stats["hardlinks"] = int(line.removeprefix("hardlink file rows: "))
    return stats


def classify(cli: Cli, planted: scenario.FullDisk) -> dict[str, Any]:
    """Run ``classify --json`` with every category listed (``--top`` has no 0)."""
    result = cli("classify", *stage_args(planted, "--top", "1000", "--json"))
    return json.loads(result.stdout)


def candidates(cli: Cli, planted: scenario.FullDisk, *, min_size: str = MIN_SIZE) -> dict[str, Any]:
    """Run ``candidates --json`` (nothing suppressed by a list cap)."""
    result = cli("candidates", *stage_args(planted, "--min-size", min_size, "--top", "0", "--json"))
    return json.loads(result.stdout)


def plan_json(
    cli: Cli,
    planted: scenario.FullDisk,
    *,
    output: Path | None = None,
    targets: Sequence[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Run ``plan`` twice: the Markdown report, then ``plan.json``.

    Both runs see the same inputs, so the plan id has to be identical -- the id
    is a function of the inputs, never of the wall clock (design section 7).
    The JSON is what the tests assert on; the Markdown is the report a human
    reads (and the demo evidence).
    """
    drives = tuple(targets) if targets is not None else (str(planted.target),)
    common = (
        *stage_args(
            planted, "--min-size", MIN_SIZE, "--top", "0", "--reserve", "0", "--free", FREE
        ),
        *[flag for drive in drives for flag in ("--to", drive)],
    )
    report = cli("plan", *common).stdout
    json_args = ("-o", str(output)) if output else ()
    result = cli("plan", *common, "--json", *json_args)
    return json.loads(result.stdout), report


def write_approval(
    cli: Cli,
    planted: scenario.FullDisk,
    plan: Mapping[str, Any],
    *,
    approved: Sequence[str],
    plan_path: Path,
    name: str = "approved.json",
) -> Path:
    """Write the approval a human would sign, with the engine's own manifest."""
    del cli  # the approval is the app's/engine's writer, not a CLI stage
    manifest = executor.make_manifest(
        str(plan["plan_id"]), list(approved), note=f"S12 acceptance run ({planted.root.name})"
    )
    return executor.write_manifest(plan_path.parent / name, manifest)


def executable_ids(plan: Mapping[str, Any]) -> list[str]:
    """The action ids of a plan that may execute (T1/T2, never advice)."""
    return [
        str(action["id"])
        for action in plan["actions"]
        if action["type"] not in ("REVIEW", "NATIVE") and action["tier"] in ("T1", "T2")
    ]


def apply_args(planted: scenario.FullDisk, plan_path: Path, approve: Path, journal: Path) -> tuple:
    """The sandboxed ``apply`` invocation: confined, quarantined, journaled."""
    return (
        str(plan_path),
        "--approve",
        str(approve),
        "--journal",
        str(journal),
        "--quarantine",
        str(planted.quarantine),
        "--within",
        str(planted.disk),
        "--json",
    )


# --------------------------------------------------------------------------- #
# The scenario itself: does the export describe the planted disk?
# --------------------------------------------------------------------------- #


def test_the_scenario_plants_a_full_disk(full_disk: scenario.FullDisk) -> None:
    """Every planted file exists with the size and age the layout asks for."""
    assert full_disk.files == len(scenario.DISK)
    assert full_disk.bytes == sum(size for size, _age in scenario.DISK.values())
    for relative, (size, age_days) in scenario.DISK.items():
        path = full_disk.disk / relative
        assert path.is_file(), f"{relative} was not planted"
        assert path.stat().st_size == size, f"{relative} has the wrong size"
        days = (os.path.getmtime(path) - full_disk.planted_at) / 86_400
        assert abs(-days - age_days) < 0.01, (
            f"{relative} is {abs(days):.2f} days old, not {age_days}"
        )

    # The duplicate pair really is one: same bytes, same size, same name.
    first, second = (full_disk.disk / relative for relative in scenario.DUPLICATES)
    assert first.name == second.name
    assert first.read_bytes() == second.read_bytes()

    # And the sub-floor files are below every threshold the pipeline uses.
    for relative in scenario.TOO_SMALL:
        assert (full_disk.disk / relative).stat().st_size < 1 * MIB


def test_the_export_describes_the_planted_tree(full_disk: scenario.FullDisk) -> None:
    """The WizTree export holds one row per entry, with folder totals that add up."""
    import csv

    with full_disk.csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_name = {row["File Name"].rstrip("/"): row for row in rows}
    assert len(by_name) == len(rows), "the export repeats a path"

    for relative, (size, _age) in scenario.DISK.items():
        key = (full_disk.disk / relative).as_posix()
        assert key in by_name, f"{relative} is missing from the export"
        assert int(by_name[key]["Size"]) == size

    # A folder row carries its descendants: the media folder is 24 + 12 MiB.
    videos = by_name[(full_disk.disk / "Users/Alice/Videos").as_posix()]
    assert int(videos["Size"]) == 24 * MIB + 12 * MIB
    assert int(videos["Files"]) == 2
    # Every ancestor of a planted file has a row too (the ingest rejects orphans).
    for relative in ("Users", "Users/Alice", "Dev/projects/shop/data"):
        assert (full_disk.disk / relative).as_posix() in by_name


# --------------------------------------------------------------------------- #
# ingest -> classify -> candidates: the index and the ranked list
# --------------------------------------------------------------------------- #


def test_ingest_indexes_exactly_what_was_planted(cli: Cli, full_disk: scenario.FullDisk) -> None:
    """The index holds the planted files, their bytes and nothing invented."""
    stats = ingest_stats(cli, full_disk, "--replace")
    planted = scenario.tree_facts(full_disk.disk)
    assert stats["files"] == planted[0] == full_disk.files
    assert stats["bytes"] == planted[2] == full_disk.bytes
    assert stats["hardlinks"] == 0
    # Rows = every entry below the tree, the tree itself, the ancestors above it
    # and the volume row the export starts with (gen_live.rows_for); the index
    # counts the volume row as a directory.
    assert stats["rows"] == stats["files"] + stats["dirs"]
    assert stats["dirs"] == planted[1] + len(full_disk.disk.parts)
    assert full_disk.index_db.is_file()


def test_classify_matches_the_disk_and_names_the_categories(
    cli: Cli, full_disk: scenario.FullDisk
) -> None:
    """Every planted file is classified; the shapes the scenario targets show up."""
    report = classify(cli, full_disk)
    totals = report["totals"]
    assert report["schema"] == "spacesage.classify/v1"
    assert totals["files"] == full_disk.files
    assert totals["matched"] + totals["unknown"] == totals["entries"]
    assert totals["matched_files"] + totals["unknown_files"] == full_disk.files
    assert totals["matched_file_bytes"] + totals["unknown_file_bytes"] == full_disk.bytes
    assert totals["unknown"] < totals["entries"], "nothing at all matched the rules"

    assert set(report["rules"]["builtin_packs"]) == {
        "browsers",
        "dev",
        "games",
        "installers",
        "media",
        "misc",
        "windows",
    }
    assert report["rules"]["user_packs"] == [], "the run must not read a user's rule packs"

    categories = {entry["category"] for entry in report["categories"]["items"]}
    # Category rows are grouped by (category, tier, action) -- build-artifacts is
    # both a T1 row (__pycache__) and a T2 row (dist) here -- so names may repeat.
    assert (
        report["categories"]["listed"]
        == report["categories"]["total"]
        == len(report["categories"]["items"])
    )
    assert len(categories) <= report["categories"]["listed"]
    for expected in (
        "windows-temp",
        "browser-cache",
        "dev-cache",
        "build-artifacts",
        "game-cache",
        "game-library",
        "installer",
        "media-video",
        "media-photos",
    ):
        assert expected in categories, f"nothing was classified as {expected}"

    # Tiers cover the three shapes: T1 scratch, T2 regenerable, T3 advice-only.
    tiers = {tier["tier"]: tier for tier in report["tiers"]}
    assert set(tiers) == {"T1", "T2", "T3"}
    assert tiers["T1"]["files"] > 0 and tiers["T3"]["files"] > 0


def test_candidates_are_real_and_ranked(cli: Cli, full_disk: scenario.FullDisk) -> None:
    """Every listed candidate names an entry that exists with the size it claims."""
    report = candidates(cli, full_disk)
    assert report["schema"] == "spacesage.candidates/v1"
    listed = [item for block in report["kinds"] for item in block["items"]]
    assert listed, "the ranked list is empty"
    assert report["summary"]["listed"] == len(listed)

    for item in listed:
        path = Path(item["path"])
        assert path.exists(), f"{item['path']} is listed but does not exist"
        assert on_disk_bytes(path) == item["bytes"], f"{item['path']} is listed with wrong bytes"
        assert item["tier"] in ("T1", "T2", "T3")
        assert item["why"], "every listed candidate explains itself"

    # Every kind the engine ranks is represented or explicitly found-empty.
    kinds = blocks(report)
    assert set(kinds) == {"delete", "move", "stale", "dupes-weak", "app"}
    for kind, block in kinds.items():
        assert block["bytes"] == sum(item["bytes"] for item in block["items"]), (
            f"the {kind} block's byte total does not match its items"
        )
        assert block["listed"] <= block["total"]
    assert kinds["dupes-weak"]["found"] == len(scenario.DUPLICATES) // 2
    assert (
        kinds["dupes-weak"]["found"] - kinds["dupes-weak"]["listed"]
        == kinds["dupes-weak"]["suppressed"]
    )

    names = {str(item["path"]) for item in listed}
    for relative in scenario.QUARANTINED_FOLDERS:
        assert covers(names, str(full_disk.disk / relative)), f"{relative} is not a candidate"
    for relative in scenario.MOVED:
        moved = {str(item["path"]) for item in items_of(report, "move")}
        assert covers(moved, str(full_disk.disk / relative)), f"{relative} is not a move candidate"
    for relative in scenario.TOO_SMALL:
        assert str(full_disk.disk / relative) not in names, f"{relative} is below the size floor"


# --------------------------------------------------------------------------- #
# plan: the course of action, and the report a human reads
# --------------------------------------------------------------------------- #


def test_the_plan_is_complete_consistent_and_safe(
    cli: Cli, full_disk: scenario.FullDisk, artifacts: Path
) -> None:
    """The plan adds up, names real entries, and never makes a T3 action executable."""
    written = artifacts / "demo-plan.json"
    plan, report = plan_json(cli, full_disk, output=written)
    assert written.is_file()

    summary = plan["summary"]
    assert plan["schema"] == "spacesage.plan/v1"
    assert str(plan["plan_id"]).startswith("sha256:")
    assert plan["targets"] == {
        str(full_disk.target): {"free_bytes": 50 * 1024**3, "reserve_bytes": 0}
    }
    assert summary["planned_bytes"] == (
        summary["delete_bytes"] + summary["move_bytes"] + summary["compress_bytes"]
    )
    assert (
        summary["actions"]
        == len(plan["actions"])
        == sum(block["actions"] for block in summary["by_type"].values())
    )
    for action_type, block in summary["by_type"].items():
        assert block["bytes"] == sum(
            action["bytes"] for action in plan["actions"] if action["type"] == action_type
        ), f"the plan's own {action_type} totals disagree"
    assert summary["native_bytes"] <= summary["review_bytes"]

    ids = [action["id"] for action in plan["actions"]]
    assert ids == [f"a{index}" for index in range(1, len(ids) + 1)], "action ids are not ordered"
    assert len(set(action_paths(plan))) == len(ids), "the plan names a path twice"

    for action in plan["actions"]:
        assert Path(action["path"]).exists(), f"{action['id']} names a missing entry"
        if action["type"] in ("DELETE_QUARANTINE", "MOVE", "COMPRESS_NTFS"):
            assert action["tier"] in ("T1", "T2"), (
                f"{action['id']} is executable but tier {action['tier']} (T3 is report-only)"
            )
        if action["type"] == "MOVE":
            dest = Path(action["dest"])
            assert dest.is_relative_to(full_disk.target), "a move leaves its target drive"
            assert not dest.is_relative_to(Path(action["path"])), "a move goes inside itself"
            assert action["link_after"] in ("SYMLINK", "JUNCTION", "HARDLINK", "NONE")

    # The five media files move; the folder rules quarantine; the sub-floor files
    # are in no plan at all.
    moves = {str(action["path"]) for action in plan["actions"] if action["type"] == "MOVE"}
    assert moves == {str(full_disk.disk / relative) for relative in scenario.MOVED}
    quarantines = {
        str(action["path"]) for action in plan["actions"] if action["type"] == "DELETE_QUARANTINE"
    }
    for relative in scenario.QUARANTINED_FOLDERS:
        assert str(full_disk.disk / relative) in quarantines
    assert not set(action_paths(plan)) & {str(full_disk.disk / rel) for rel in scenario.TOO_SMALL}

    # The Markdown report is the same plan: same id, same totals, every action.
    assert str(plan["plan_id"]) in report
    assert "actions" in report.splitlines()[0]
    for action in plan["actions"]:
        assert f"`{action['id']}`" in report
        assert str(action["path"]) in report


def test_two_plan_runs_over_the_same_index_have_the_same_id(
    cli: Cli, full_disk: scenario.FullDisk
) -> None:
    """The plan id is a function of the inputs, not of the wall clock."""
    first, _report = plan_json(cli, full_disk)
    second, _other = plan_json(cli, full_disk)
    assert first["plan_id"] == second["plan_id"]
    assert [action["id"] for action in first["actions"]] == [
        action["id"] for action in second["actions"]
    ]


# --------------------------------------------------------------------------- #
# apply -> undo: the dry run, the run, and the restoration
# --------------------------------------------------------------------------- #


def test_dry_run_execute_and_undo_restore_the_disk(
    cli: Cli, mutation_disk: scenario.FullDisk, evidence
) -> None:
    """The whole loop: nothing moves, then everything moves, then everything is back."""
    before = scenario.snapshot(mutation_disk.disk)
    ingest_stats(cli, mutation_disk, "--replace")
    plan, _report = plan_json(cli, mutation_disk, output=mutation_disk.root / "plan.json")
    plan_path = mutation_disk.root / "plan.json"
    approved = executable_ids(plan)
    assert approved, "the scenario has nothing to execute"
    approve = write_approval(cli, mutation_disk, plan, approved=approved, plan_path=plan_path)

    # -- the dry run: the resolved operations, and a disk that does not move --- #
    dry_journal = mutation_disk.root / "dry-run.jsonl"
    dry = cli("apply", *apply_args(mutation_disk, plan_path, approve, dry_journal))
    dry_report = json.loads(dry.stdout)
    assert dry_report["mode"] == "dry-run"
    assert dry_report["counts"]["planned"] == len(approved)
    assert dry_report["counts"]["done"] == 0
    assert dry_report["reclaimed_bytes"] == plan["summary"]["planned_bytes"]
    assert dry_report["total_actions"] == len(plan["actions"])
    assert scenario.snapshot(mutation_disk.disk) == before, "the dry run touched the disk"

    # A dry run journals nothing that could be reversed: undo has nothing to do.
    dry_undo = cli("undo", str(dry_journal), "--json")
    assert json.loads(dry_undo.stdout)["ops"] == []

    # -- the run ------------------------------------------------------------- #
    journal = mutation_disk.root / "journal.jsonl"
    executed = cli("apply", *apply_args(mutation_disk, plan_path, approve, journal), "--execute")
    run = json.loads(executed.stdout)
    assert run["mode"] == "execute"
    assert run["counts"]["done"] == len(approved)
    assert run["counts"]["failed"] == 0 and run["counts"]["refused"] == 0
    assert run["reclaimed_bytes"] == plan["summary"]["planned_bytes"]
    assert journal.is_file() and journal.stat().st_size > 0

    assert scenario.snapshot(mutation_disk.disk) != before, "the execute changed nothing"
    gone = {
        str(action["path"]) for action in plan["actions"] if action["type"] == "DELETE_QUARANTINE"
    }
    for path in gone:
        assert not Path(path).exists(), f"{path} should be quarantined"
    for action in plan["actions"]:
        if action["type"] != "MOVE":
            continue
        source = mutation_disk.disk / Path(action["path"]).relative_to(mutation_disk.disk)
        assert source.is_symlink(), f"{source} is not linked back"
        assert source.resolve() == Path(action["dest"]).resolve()
        assert Path(action["dest"]).stat().st_size == action["bytes"], "a moved payload is short"
    # The bytes the run reports really left the tree.
    moved_bytes = sum(
        action["bytes"] for action in plan["actions"] if action["type"] == "MOVE"
    ) + sum(action["bytes"] for action in plan["actions"] if action["type"] == "DELETE_QUARANTINE")
    assert moved_bytes == run["reclaimed_bytes"]

    # -- the undo ------------------------------------------------------------ #
    undone = cli("undo", str(journal), "--json")
    undo = json.loads(undone.stdout)
    assert undo["counts"]["failed"] == 0 and undo["counts"]["blocked"] == 0
    assert undo["counts"]["done"] > len(approved), "moves also reverse their links"
    assert undo["restored_bytes"] == run["reclaimed_bytes"], "undo did not restore the same bytes"
    assert scenario.snapshot(mutation_disk.disk) == before, "the restored tree differs"

    # Running undo twice is safe: everything is already reversed.
    twice = json.loads(cli("undo", str(journal), "--json").stdout)
    assert twice["ops"] == []
    assert twice["already_undone"] == undo["counts"]["done"]

    evidence(
        "pipeline-run.txt",
        "\n".join(
            [
                f"plan id          {plan['plan_id']}",
                f"approved         {len(approved)} of {len(plan['actions'])} actions",
                f"planned bytes    {plan['summary']['planned_bytes']}",
                f"reclaimed bytes  {run['reclaimed_bytes']}",
                f"restored bytes   {undo['restored_bytes']}",
                f"journal          {journal}",
                f"tree before      {len(before)} entries",
                f"tree after undo  {len(scenario.snapshot(mutation_disk.disk))} entries"
                " (identical)",
                "",
                "commands:",
                *(
                    "  spacesage " + " ".join(shlex.quote(part) for part in call)
                    for call in cli.calls
                ),
            ]
        ),
    )


def test_the_executor_gates_hold(cli: Cli, mutation_disk: scenario.FullDisk) -> None:
    """Approvals cannot be borrowed, T3 cannot be executed, and the sandbox holds."""
    before = scenario.snapshot(mutation_disk.disk)
    ingest_stats(cli, mutation_disk, "--replace")
    plan, _report = plan_json(cli, mutation_disk, output=mutation_disk.root / "plan.json")
    plan_path = mutation_disk.root / "plan.json"
    approved = executable_ids(plan)
    honest = write_approval(
        cli, mutation_disk, plan, approved=approved, plan_path=plan_path, name="honest.json"
    )
    journal = mutation_disk.root / "gates.jsonl"

    # 1. A manifest written for another plan id is refused: no execution, no trace.
    foreign = dict(plan)
    foreign["plan_id"] = "sha256:" + "0" * 64
    foreign_approve = write_approval(
        cli, mutation_disk, foreign, approved=approved, plan_path=plan_path, name="foreign.json"
    )
    refused = cli(
        "apply",
        *apply_args(mutation_disk, plan_path, foreign_approve, journal),
        "--execute",
        check=False,
    )
    assert refused.returncode == 1
    assert "manifest approves" in refused.stderr
    assert not journal.exists(), "a refused run must not journal anything"

    # 2. Advice can be approved (the manifest only names ids) but never runs.
    advice = [
        str(action["id"]) for action in plan["actions"] if action["type"] in ("REVIEW", "NATIVE")
    ]
    assert advice, "the scenario has no advice items"
    advice_approve = write_approval(
        cli, mutation_disk, plan, approved=advice, plan_path=plan_path, name="advice.json"
    )
    advice_run = json.loads(
        cli("apply", *apply_args(mutation_disk, plan_path, advice_approve, journal)).stdout
    )
    assert advice_run["counts"]["planned"] == 0
    assert advice_run["counts"]["skipped"] == len(advice)
    assert advice_run["reclaimed_bytes"] == 0
    assert all(op["advisory"] for op in advice_run["ops"])

    # 3. The --within sandbox refuses operations outside the root it was given.
    elsewhere = mutation_disk.root / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    confined = cli(
        "apply",
        str(plan_path),
        "--approve",
        str(honest),
        "--journal",
        str(journal),
        "--quarantine",
        str(mutation_disk.quarantine),
        "--within",
        str(elsewhere),
        "--json",
        check=False,
    )
    confined_report = json.loads(confined.stdout)
    assert confined_report["counts"]["refused"] == len(approved)
    assert confined.returncode == 1, "a run that refused everything is not ok"
    assert all(
        "confined" in op["reason"] or "outside" in op["reason"] for op in confined_report["ops"]
    )
    assert scenario.snapshot(mutation_disk.disk) == before, "a confined run still touched the disk"

    # 4. It also refuses a plan whose ids it does not have.
    plan_data = json.loads(plan_path.read_text())
    plan_data["actions"] = plan_data["actions"][:1]
    trimmed = mutation_disk.root / "trimmed.json"
    trimmed.write_text(json.dumps(plan_data), encoding="utf-8")
    unknown = cli(
        "apply", *apply_args(mutation_disk, trimmed, honest, journal), "--execute", check=False
    )
    assert unknown.returncode == 1
    assert scenario.snapshot(mutation_disk.disk) == before, "a rejected plan touched the disk"


# --------------------------------------------------------------------------- #
# deepscan: the verified duplicate story behind the weak-dupe kind
# --------------------------------------------------------------------------- #


def test_deepscan_verifies_the_duplicate_pair(cli: Cli, full_disk: scenario.FullDisk) -> None:
    """The hash-verified group matches the pair the scenario planted."""
    import hashlib

    result = cli("deepscan", str(full_disk.disk), "--min-size", MIN_SIZE, "--json")
    report = json.loads(result.stdout)
    assert report["schema"] == "spacesage.deepscan/v1"
    assert report["roots"] == [str(full_disk.disk)]
    wanted = {str(full_disk.disk / relative) for relative in scenario.DUPLICATES}
    groups = [
        group
        for group in report["groups"]
        if {str(member["path"]) for member in group["members"]} == wanted
    ]
    assert len(groups) == 1, f"the planted duplicate pair is not one verified group: {groups}"

    group = groups[0]
    first = full_disk.disk / scenario.DUPLICATES[0]
    digest = hashlib.sha256()
    with first.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    assert group["sha256"] == digest.hexdigest(), "the group is not the pair's own hash"
    assert group["copies"] == 2 and group["paths"] == 2
    assert group["size"] == first.stat().st_size
    assert group["reclaimable_bytes"] == first.stat().st_size, "one copy is recoverable"
    roles = {str(member["path"]): member["role"] for member in group["members"]}
    assert sorted(roles.values()) == ["duplicate", "keep"], "one copy survives, one is redundant"
    assert group["keep"] in wanted and roles[str(group["keep"])] == "keep"
    assert report["hardlink_sets"] == []
    assert report["summary"]["reclaimable_bytes"] == first.stat().st_size


def test_every_stage_agrees_on_the_numbers(cli: Cli, full_disk: scenario.FullDisk) -> None:
    """The same bytes travel through every stage without drifting."""
    stats = ingest_stats(cli, full_disk, "--replace")
    report = classify(cli, full_disk)
    ranked = candidates(cli, full_disk)
    plan, _markdown = plan_json(cli, full_disk)

    planted = scenario.tree_facts(full_disk.disk)
    assert stats["bytes"] == planted[2]
    assert report["totals"]["files"] == stats["files"]
    assert (
        report["totals"]["matched_file_bytes"] + report["totals"]["unknown_file_bytes"]
        == (stats["bytes"])
    )
    listed = [item for block in ranked["kinds"] for item in block["items"]]
    assert len(listed) == ranked["summary"]["listed"]
    assert all(Path(item["path"]).exists() for item in listed)

    # The plan can only hold actions for entries the ranked list named (a folder
    # candidate may cover its children, so the check goes downwards).
    keys = {str(item["path"]) for item in listed}
    for path in action_paths(plan):
        assert covers(keys, path), f"{path} is planned but was never ranked"

    # One action per thing: nothing executable sits inside something else that is
    # executable (an advisory REVIEW/NATIVE parent may overlap -- it does nothing).
    executable = [
        str(action["path"])
        for action in plan["actions"]
        if action["type"] in planner.EXECUTABLE_TYPES
    ]
    for path in executable:
        assert not any(
            other != path and path.startswith(other.rstrip("/") + "/") for other in executable
        ), f"{path} is nested inside another executable action"
    planned = plan["summary"]["planned_bytes"]
    assert planned <= stats["bytes"]
    assert plan["summary"]["delete_bytes"] == blocks(ranked)["delete"]["bytes"]
    assert plan["summary"]["review_bytes"] + plan["summary"]["planned_bytes"] <= stats["bytes"]


def test_the_scenario_planted_at_the_reference_path(full_disk: scenario.FullDisk) -> None:
    """The disk sits where the export says, and the target is on the second volume."""
    assert full_disk.root == scenario.DEFAULT_ROOT
    assert full_disk.disk == full_disk.root / "disk"
    assert full_disk.disk.is_dir()
    assert planner.volume_of(str(full_disk.disk)) != planner.volume_of(str(full_disk.target))
    assert full_disk.target.is_dir(), "a move needs a destination that exists"
