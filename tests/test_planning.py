"""The planning seam: selection -> draft -> approval -> run -> undo.

The scenario is the committed ``tests/fixtures/data/candidates.csv`` (the same
export the ranked list is tested against), plus a *live* sandbox
(``tests/fixtures/gen_live.py``) for the runs that really touch a filesystem:
draft, write the workspace, preview, execute, revert and compare the tree.

The point of these tests is the contract the Plan and Undo screens lean on:
the draft holds the checked rows and nothing else, the warnings say what the
composer actually decided, an approval can only name actions of this very plan,
a preview touches nothing, and an undo restores the tree byte for byte.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fixtures import gen_executor, gen_live
from spacesage import db, executor, opportunities, planner, planning, rules
from spacesage.ingest import ingest_csv

DATA_DIR = Path(__file__).resolve().parent / "fixtures" / "data"

#: Reference point of the fixture (2026-09-12 12:00:00 UTC).
NOW = int(datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC).timestamp())
MIB = 1024**2
GIB = 1024**3
MIN = 100 * MIB

VIDEOS = "C:\\Users\\Alice\\Videos"
VIDEOS_CLIP = "C:\\Users\\Alice\\Videos\\holiday.mp4"
CHROME_CACHE = "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache"
BIG_DMP = "C:\\Users\\Alice\\Documents\\big.dmp"
ARCHIVE = "C:\\Users\\Alice\\Archive"
WINDOWS = "C:\\Windows"
WIDGET_EXE = "C:\\Program Files\\Widget\\widget.exe"
TEMP = "C:\\Windows\\Temp"
DOWNLOADS = "C:\\Users\\Alice\\Downloads"

SOURCE: dict[str, str] = {
    "csv": "fixture.csv",
    "machine": "FIXTURE-PC",
    "exported": "2026-09-12T12:00:00+00:00",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def index_of_fixture(tmp_path: Path) -> Path:
    """Ingest the committed candidate fixture into a fresh index."""
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / "candidates.csv", db_path)
    return db_path


def listing_for(db_path: Path, *, min_size: int = MIN) -> opportunities.OpportunityList:
    """The ranked list of an index, with the fixture's reference moment."""
    conn = db.open_db(db_path)
    try:
        return opportunities.build_opportunities(
            conn,
            rules.load_rules(include_user=False),
            min_size=min_size,
            list_top=500,
            explicit=250,
            now=NOW,
            db_path=str(db_path),
        )
    finally:
        conn.close()


def draft_for(
    db_path: Path,
    listing: opportunities.OpportunityList,
    *paths: str,
    targets: tuple[planner.PlanTarget, ...] = (),
    links: bool = True,
) -> planning.PlanDraft:
    """Draft the plan of a selection against the fixture index."""
    conn = db.open_db(db_path)
    try:
        request = planning.PlanRequest.from_listing(
            listing, list(paths), targets=targets, links=links
        )
        return planning.draft_plan(conn, rules.load_rules(include_user=False), request)
    finally:
        conn.close()


def d_target(free: int = 200 * GIB, reserve: int = 20 * GIB) -> planner.PlanTarget:
    """The roomy ``D:`` target the fixture plans against."""
    return planner.PlanTarget(name="D:", free_bytes=free, reserve_bytes=reserve)


def paths_of(draft: planning.PlanDraft) -> list[str]:
    """Every action path of a draft, in plan order."""
    return [item.action.path for item in draft.items]


# --------------------------------------------------------------------------- #
# The draft
# --------------------------------------------------------------------------- #


def test_the_draft_holds_exactly_the_checked_rows(tmp_path: Path) -> None:
    """A selection is a scope: nothing outside it may appear in the plan."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, BIG_DMP, CHROME_CACHE, targets=(d_target(),))

    assert paths_of(draft) == [CHROME_CACHE, BIG_DMP]
    assert [item.action.type for item in draft.items] == ["DELETE_QUARANTINE"] * 2
    assert all(item.origin == item.action.path for item in draft.items)
    assert draft.executable_ids() == (draft.items[0].action.id, draft.items[1].action.id)
    assert not draft.warnings_of("blocker")
    assert draft.unplanned == ()

    # The other fixture paths are simply not in this plan.
    listed = json.dumps(draft.plan.to_dict())
    for absent in (VIDEOS, ARCHIVE, TEMP):
        assert absent not in listed


def test_the_draft_reuses_the_listing_thresholds(tmp_path: Path) -> None:
    """The plan draws from the same candidates the screen showed."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path, min_size=10 * MIB)
    request = planning.PlanRequest.from_listing(listing, [BIG_DMP], targets=(d_target(),))

    assert request.min_size == 10 * MIB
    assert request.list_top == listing.list_top
    assert request.kinds == tuple(listing.kinds)
    assert request.now == float(listing.as_of)
    assert request.db_path == listing.db_path


def test_a_checked_folder_covers_its_contents(tmp_path: Path) -> None:
    """One row per branch: checking a folder plans the folder, not its files."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, VIDEOS, targets=(d_target(),))

    assert paths_of(draft) == [VIDEOS]
    action = draft.items[0].action
    assert action.type == "MOVE"
    assert action.dest is not None and action.dest.startswith("D:\\Moved\\Users\\Alice\\Videos")
    assert action.link_after == "JUNCTION"
    assert draft.unplanned == ()


def test_an_entry_the_candidates_never_listed_is_reported_not_planned(tmp_path: Path) -> None:
    """The biggest-entries scan adds rows that carry no plan action: say so."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    # ``holiday.mp4`` is an explicit scan row; the folder *is* the move candidate.
    draft = draft_for(db_path, listing, VIDEOS_CLIP, targets=(d_target(),))

    assert draft.items == ()
    assert draft.unplanned == (VIDEOS_CLIP,)
    assert "nothing to plan" in " ".join(
        warning.message.lower() for warning in draft.warnings_of("blocker")
    )
    assert draft.warnings_of("info")


def test_a_no_action_folder_still_covers_the_actions_inside_it(tmp_path: Path) -> None:
    """A checked folder means its whole branch: the actions below it come along.

    ``C:\\Windows`` is a "No action" row (the rules leave the folder itself
    alone), yet the list shows ``C:\\Windows\\Temp`` inside it as an actionable
    row -- checking the folder is a statement about the branch, and every item
    says which checked row it came from.
    """
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, WINDOWS)

    assert paths_of(draft) == [TEMP]
    assert draft.items[0].origin == WINDOWS
    assert draft.items[0].action.type == "DELETE_QUARANTINE"
    assert draft.unplanned == ()


def test_a_selection_with_no_candidate_at_all_is_a_blocker(tmp_path: Path) -> None:
    """A row the rules leave alone, with nothing actionable anywhere below it."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, WIDGET_EXE)

    assert draft.items == ()
    assert draft.refused_ids() == ()
    blockers = draft.warnings_of("blocker")
    assert blockers and "nothing to plan" in blockers[0].message.lower()
    assert draft.unplanned == (WIDGET_EXE,)
    assert draft.summary_line().startswith("0 executable actions")


def test_a_move_without_room_is_a_budget_warning(tmp_path: Path) -> None:
    """The composer demotes the move; the screen has to say *why*, distinctly."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, VIDEOS, targets=(d_target(free=1 * MIB, reserve=0),))

    assert [item.action.type for item in draft.items] == ["REVIEW"]
    budget = draft.warnings_of("budget")
    assert budget, [warning.to_dict() for warning in draft.warnings]
    assert "no target drive has room" in budget[0].message.lower()
    assert "needs" in budget[0].message  # the engine's own numbers stay in it
    assert budget[0].paths == (VIDEOS,)
    assert budget[0].label == "No room"
    assert not draft.executable_ids()


def test_a_target_on_the_source_drive_is_a_conflict(tmp_path: Path) -> None:
    """A move to the same volume frees nothing: the plan refuses it by design."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(
        db_path,
        listing,
        VIDEOS,
        targets=(planner.PlanTarget(name="C:", free_bytes=500 * GIB, reserve_bytes=0),),
    )

    conflicts = draft.warnings_of("conflict")
    assert conflicts and "source's own drive" in conflicts[0].message
    assert [item.action.type for item in draft.items] == ["REVIEW"]


def test_a_selection_without_a_target_says_so_once(tmp_path: Path) -> None:
    """No target drive at all: one conflict naming the moves, not one per move."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, VIDEOS)

    conflicts = draft.warnings_of("conflict")
    assert len(conflicts) == 1
    assert "No target drive is set" in conflicts[0].message
    assert draft.items and all(item.action.type == "REVIEW" for item in draft.items)


def test_a_nested_selection_is_reported_as_a_conflict(tmp_path: Path) -> None:
    """Checking a folder *and* something inside it is counted once -- and said."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, VIDEOS, VIDEOS_CLIP, targets=(d_target(),))

    conflicts = draft.warnings_of("conflict")
    assert any("sits inside" in warning.message for warning in conflicts)
    assert "counted once" in conflicts[0].message


def test_an_advisory_selection_is_advice_not_an_executable_plan(tmp_path: Path) -> None:
    """A T3 review item is listed with its advice and never becomes runnable."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, ARCHIVE)

    assert [item.action.type for item in draft.items] == ["REVIEW"]
    item = draft.items[0]
    assert item.status == "advice" and not item.executable
    assert draft.executable_ids() == ()
    assert draft.advisory_ids() == (item.action.id,)
    assert any("only holds advice" in warning.message for warning in draft.warnings_of("blocker"))


def test_warnings_are_ordered_by_severity(tmp_path: Path) -> None:
    """Blockers read first; the screen paints them in that order."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, WIDGET_EXE, VIDEOS, VIDEOS_CLIP)

    severities = [warning.severity for warning in draft.warnings]
    ranks = [planning.SEVERITIES.index(severity) for severity in severities]
    assert ranks == sorted(ranks)
    assert ranks[0] == 0  # the blocker leads
    assert severities.count("conflict") >= 1


def test_refusals_surface_as_blocker_warnings() -> None:
    """A refused action (protected path) is a blocker before anything is approved."""
    action = gen_executor.action("a1", "DELETE_QUARANTINE", WINDOWS, 10 * MIB, kind="delete")
    plan = gen_executor.plan_dict([action], source=SOURCE)
    report = planning.preview_plan(plan, ["a1"])
    items = (
        planning.PlanItem(
            action=planner.PlanAction(
                id="a1",
                type="DELETE_QUARANTINE",
                kind="delete",
                path=WINDOWS,
                bytes=10 * MIB,
                category="x",
                tier="T1",
                confidence=0.9,
                rationale="r",
                why="w",
            ),
            origin=WINDOWS,
            executable=True,
            detail="",
        ),
    )
    warnings = planning.warnings_from_preview(items, report)
    assert report.ops[0].outcome == "refused"
    assert warnings and warnings[0].severity == "blocker"
    assert "the system directory Windows" in warnings[0].message


# --------------------------------------------------------------------------- #
# The approval and the workspace
# --------------------------------------------------------------------------- #


def test_write_draft_persists_the_plan_and_a_bound_approval(tmp_path: Path) -> None:
    """``plan.json`` + ``approved.json`` behind the scenes, bound by plan id."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, BIG_DMP, ARCHIVE, targets=(d_target(),))
    workspace = planning.write_draft(draft, tmp_path / "data")

    assert workspace.directory.name == executor.plan_token(draft.plan_id)
    assert workspace.plan_path.is_file() and workspace.manifest_path.is_file()
    document = json.loads(workspace.plan_path.read_text(encoding="utf-8"))
    planner.validate_plan(document)
    assert document["plan_id"] == draft.plan_id

    manifest = planning.load_approval(workspace)
    assert manifest is not None
    assert manifest.plan_id == draft.plan_id
    assert manifest.approved == draft.executable_ids()  # advice is never approved
    assert manifest.rejected == draft.advisory_ids()


def test_an_approval_can_only_name_actions_of_this_plan(tmp_path: Path) -> None:
    """The manifest is the gate: unknown ids and advice items are refused."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, BIG_DMP, ARCHIVE, targets=(d_target(),))
    workspace = planning.write_draft(draft, tmp_path / "data")

    with pytest.raises(planning.PlanningError, match="does not have"):
        planning.save_approval(workspace, draft, ["a7"])
    with pytest.raises(planning.PlanningError, match="advice items"):
        planning.save_approval(workspace, draft, draft.advisory_ids())


def test_saving_an_approval_rewrites_the_manifest_in_place(tmp_path: Path) -> None:
    """Approve/reject is one manifest rewrite -- same file, same plan id."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, BIG_DMP, CHROME_CACHE, targets=(d_target(),))
    workspace = planning.write_draft(draft, tmp_path / "data")
    first, second = draft.executable_ids()

    planning.save_approval(workspace, draft, [first], rejected=[second])
    manifest = planning.load_approval(workspace)
    assert manifest is not None
    assert manifest.approved == (first,) and manifest.rejected == (second,)


def test_a_workspace_of_another_plan_is_refused(tmp_path: Path) -> None:
    """An approval can never cross plans (a stale approval must not run)."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    first = draft_for(db_path, listing, BIG_DMP, targets=(d_target(),))
    second = draft_for(db_path, listing, CHROME_CACHE, targets=(d_target(),))
    workspace = planning.write_draft(first, tmp_path / "data")

    with pytest.raises(planning.PlanningError, match="the workspace holds plan"):
        planning.save_approval(workspace, second, second.executable_ids())
    with pytest.raises(planning.PlanningError, match="the workspace holds plan"):
        planning.execute(second, workspace, second.executable_ids())


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #


def test_the_preview_resolves_every_action_and_touches_nothing(tmp_path: Path) -> None:
    """The dry run is the plan screen's promise: exact operations, no changes."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    draft = draft_for(db_path, listing, VIDEOS, targets=(d_target(),))
    workspace = planning.write_draft(draft, tmp_path / "data")

    report = planning.preview(draft, draft.executable_ids())
    assert report.dry_run and report.ok()
    op = report.ops[0]
    assert op.outcome == "planned"
    step = op.steps[0]
    assert step.op == "move" and step.dest is not None and step.dest.startswith("D:\\Moved\\")
    assert step.link == "JUNCTION"

    # Nothing was written anywhere: no journal, no quarantine store.
    assert not workspace.journal_path.exists()
    quarantines = list(tmp_path.rglob("_spacesage_quarantine*"))
    assert quarantines == []


def test_the_preview_reports_refusals_without_running_them(tmp_path: Path) -> None:
    """A protected path is refused in the preview -- visibly, not silently."""
    action = gen_executor.action("a1", "DELETE_QUARANTINE", WINDOWS, 10 * MIB, kind="delete")
    plan = gen_executor.plan_dict([action], source=SOURCE)
    report = planning.preview_plan(plan, ["a1"])
    assert report.ops[0].outcome == "refused"
    assert "system directory" in report.ops[0].reason
    assert not report.ok()


# --------------------------------------------------------------------------- #
# The live loop: execute against a sandbox tree, then revert it
# --------------------------------------------------------------------------- #


def _foreign_root() -> Path:
    """A writable temporary folder that is *not* on ``/tmp``'s volume.

    The planner refuses a target on the source's own volume (a move there frees
    nothing), so the live tests need a second one.  ``/var/tmp`` is on a
    different first component than ``/tmp`` -- which is all
    :func:`spacesage.planner.same_volume` looks at -- and exists on every Linux
    runner; ``/dev/shm`` is the fallback.  A runner with neither skips.
    """
    import tempfile

    for candidate in ("/var/tmp", "/dev/shm"):
        root = Path(candidate)
        if not root.is_dir():
            continue
        try:
            return Path(tempfile.mkdtemp(prefix="spacesage-live-", dir=str(root)))
        except OSError:  # pragma: no cover - a read-only tmp area
            continue
    pytest.skip("no writable temporary area on another volume")  # pragma: no cover
    raise AssertionError("unreachable")


@dataclass
class Sandbox:
    """The planted tree, its export, an index of it and a target on another volume."""

    live: gen_live.Live
    listing: opportunities.OpportunityList
    db_path: Path
    target: Path
    quarantine: Path
    foreign: Path

    def target_spec(self) -> planner.PlanTarget:
        """The target drive moves are planned onto (roomy, no reserve)."""
        return planner.PlanTarget(name=str(self.target), free_bytes=10 * GIB, reserve_bytes=0)

    def selected(self, *, tier: str | None = None, limit: int | None = None) -> list[str]:
        """The sandbox's actionable paths (optionally one tier only)."""
        rows = [
            row
            for row in self.listing.rows
            if row.state == opportunities.STATE_ACTION and not row.advisory
        ]
        if tier is not None:
            rows = [row for row in rows if row.tier == tier]
        paths = [row.path for row in rows]
        return paths if limit is None else paths[:limit]

    def draft(self, *paths: str, targets: bool = True) -> planning.PlanDraft:
        """Draft a plan for ``paths`` against this sandbox."""
        conn = db.open_db(self.db_path)
        try:
            request = planning.PlanRequest.from_listing(
                self.listing,
                list(paths),
                targets=(self.target_spec(),) if targets else (),
            )
            return planning.draft_plan(conn, rules.load_rules(include_user=False), request)
        finally:
            conn.close()


@pytest.fixture
def sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    """A live sandbox tree, indexed, with a target drive on another volume."""
    foreign = _foreign_root()
    if foreign is None:  # pragma: no cover - every CI runner has one
        pytest.skip("no writable temporary area on another volume")
    live = gen_live.scenario(tmp_path / "sandbox")
    ingest_csv(live.csv_path, live.db)
    conn = db.open_db(live.db)
    try:
        listing = opportunities.build_opportunities(
            conn,
            rules.load_rules(include_user=False),
            min_size=1 * MIB,
            now=NOW,
            db_path=str(live.db),
        )
    finally:
        conn.close()
    try:
        yield Sandbox(
            live=live,
            listing=listing,
            db_path=live.db,
            target=foreign / "target",
            quarantine=tmp_path / "sandbox" / "quarantine",
            foreign=foreign,
        )
    finally:
        gen_live.cleanup(foreign)


def test_the_sandbox_is_what_the_rules_think_it_is(sandbox: Sandbox) -> None:
    """The fixture's premise: one folder delete, two file deletes, one move, one gap."""
    by_path = {row.path: row for row in sandbox.listing.rows}
    assert by_path[str(sandbox.live.tree / "app" / "node_modules")].action == "DELETE_QUARANTINE"
    assert by_path[str(sandbox.live.tree / "scratch" / "old.dmp")].action == "DELETE_QUARANTINE"
    assert by_path[str(sandbox.live.tree / "media" / "holiday.mp4")].action == "MOVE"
    # ``notes.txt`` matches no rule: it is the list's "undecided" gap, and the
    # plan must never invent an action for it.
    assert by_path[str(sandbox.live.tree / "keep" / "notes.txt")].state == (
        opportunities.STATE_UNDECIDED
    )


def test_the_full_loop_executes_and_restores_the_tree(sandbox: Sandbox) -> None:
    """Select the sandbox's wins, plan, preview, execute, revert -- byte for byte."""
    before = gen_live.snapshot(sandbox.live.tree)
    draft = sandbox.draft(*sandbox.selected())
    assert draft.executable_ids(), draft.to_dict()
    assert not draft.warnings_of("blocker"), [warning.to_dict() for warning in draft.warnings]
    workspace = planning.write_draft(draft, sandbox.live.csv_path.parent / "data")

    # The preview resolves the same operations the run then performs.
    preview = planning.preview(draft, draft.executable_ids(), quarantine_root=sandbox.quarantine)
    assert preview.ok(), preview.render_text()
    assert all(op.outcome == "planned" for op in preview.ops)
    assert gen_live.snapshot(sandbox.live.tree) == before

    progress: list[tuple[int, int, str]] = []
    report = planning.execute(
        draft,
        workspace,
        draft.executable_ids(),
        quarantine_root=sandbox.quarantine,
        on_op=lambda result, index, total: progress.append((index, total, result.outcome)),
    )
    assert report.ok(), report.render_text()
    assert [entry[2] for entry in progress] == ["done"] * len(draft.executable_ids())
    assert progress[-1][0] == progress[-1][1] == len(draft.executable_ids())
    assert gen_live.snapshot(sandbox.live.tree) != before  # the tree really changed

    history = planning.journal_history(workspace.journal_path)
    assert history.plan_id == draft.plan_id
    assert history.pending(), history.to_dict()
    assert history.counts()["pending"] == len(history.items)
    assert history.reclaimed_bytes() > 0

    undone = planning.revert(history)
    assert undone.ok(), undone.to_dict()
    assert gen_live.snapshot(sandbox.live.tree) == before

    after = planning.journal_history(workspace.journal_path)
    assert not after.pending()
    assert after.counts()["reversed"] == len(after.items)
    assert after.restored_bytes() == history.reclaimed_bytes()


def test_a_subset_revert_reverses_only_what_was_picked(sandbox: Sandbox) -> None:
    """One-click revert works per item too; the rest stays pending."""
    draft = sandbox.draft(*sandbox.selected(tier="T1"))
    assert len(draft.executable_ids()) >= 2
    workspace = planning.write_draft(draft, sandbox.live.csv_path.parent / "data")
    report = planning.execute(
        draft, workspace, draft.executable_ids(), quarantine_root=sandbox.quarantine
    )
    assert report.ok(), report.render_text()

    history = planning.journal_history(workspace.journal_path)
    pending = history.pending()
    assert len(pending) >= 2
    oldest = pending[-1]  # the first operation that still has to be reversed

    undone = planning.revert(history, only=[oldest.seq])
    assert undone.ok(), undone.to_dict()
    assert [result.op_ref for result in undone.ops] == [oldest.seq]

    after = planning.journal_history(workspace.journal_path)
    assert len(after.pending()) == len(pending) - 1
    assert next(item for item in after.items if item.seq == oldest.seq).status == "reversed"


def test_a_second_revert_finds_nothing_to_do(sandbox: Sandbox) -> None:
    """Running undo twice is safe -- the screen's "revert" button stays honest."""
    draft = sandbox.draft(*sandbox.selected(tier="T1"))
    workspace = planning.write_draft(draft, sandbox.live.csv_path.parent / "data")
    planning.execute(draft, workspace, draft.executable_ids(), quarantine_root=sandbox.quarantine)

    first = planning.revert(planning.journal_history(workspace.journal_path))
    assert first.ops
    second = planning.revert(planning.journal_history(workspace.journal_path))
    assert second.ops == ()
    assert second.already_undone >= len(first.ops)


def test_histories_lists_the_workspaces_newest_first(sandbox: Sandbox) -> None:
    """The Undo screen's list: every plan that has a journal, newest first."""
    root = sandbox.live.csv_path.parent / "data"
    first = sandbox.draft(*sandbox.selected(tier="T1", limit=1))
    second = sandbox.draft(*sandbox.selected(tier="T2", limit=1))
    one = planning.write_draft(first, root)
    two = planning.write_draft(second, root)
    planning.execute(first, one, first.executable_ids(), quarantine_root=sandbox.quarantine)
    planning.execute(second, two, second.executable_ids(), quarantine_root=sandbox.quarantine)

    found = planning.histories(root)
    assert {history.path for history in found} == {one.journal_path, two.journal_path}
    assert all(history.plan_id for history in found)


def test_a_broken_journal_is_reported_not_raised(tmp_path: Path) -> None:
    """The picker has to *show* a journal it cannot read, not crash on it."""
    path = tmp_path / "journal.jsonl"
    path.write_text("{not json at all\n", encoding="utf-8")
    history = planning.journal_history(path)

    assert history.error and "journal" in history.error
    assert history.items == ()
    assert history.counts()["pending"] == 0


def test_an_empty_selection_is_refused(tmp_path: Path) -> None:
    """A plan of nothing is a bug, not a document: refuse it loudly."""
    db_path = index_of_fixture(tmp_path)
    listing = listing_for(db_path)
    with pytest.raises(planning.PlanningError, match="no items selected"):
        draft_for(db_path, listing)
