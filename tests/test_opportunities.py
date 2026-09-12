"""Opportunities view-model tests: rows, states, cascade, filters, summary.

The scenario is the committed fixture ``tests/fixtures/data/candidates.csv``
(the same one the candidate tests use), plus hand-built rows for the pure
arithmetic (cascade, sorting, dedup), so every assertion is either an engine
cross-check or small enough to read by hand.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spacesage import db, opportunities, rules
from spacesage.ingest import ingest_csv
from spacesage.opportunities import (
    STATE_ACTION,
    STATE_NO_ACTION,
    STATE_UNDECIDED,
    OpportunitiesError,
    Opportunity,
    OpportunityFilter,
    Selection,
    advisory_note,
    alternatives,
    apply_filter,
    build_opportunities,
    destination_for,
    link_for,
    rows_from_dicts,
    side_effects,
    sort_rows,
    summarise,
    top_level_rows,
)

DATA_DIR = Path(__file__).resolve().parent / "fixtures" / "data"

#: Reference point of the fixture and the tests (2026-09-12 12:00:00 UTC).
NOW = int(datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC).timestamp())
MIB = 1024**2
MIN = 100 * MIB

VIDEOS = "C:\\Users\\Alice\\Videos"
VIDEOS_CLIP = "C:\\Users\\Alice\\Videos\\holiday.mp4"
CHROME_CACHE = "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache"
USER_TEMP = "C:\\Users\\Alice\\AppData\\Local\\Temp"
WIDGET = "C:\\Program Files\\Widget"
WINDOWS = "C:\\Windows"
BIG_DMP = "C:\\Users\\Alice\\Documents\\big.dmp"
DUP_DOCS = "C:\\Users\\Alice\\Documents\\dataset.bin"
DUP_VAULT = "C:\\Users\\Alice\\Vault\\dataset.bin"
UNKNOWN_FILE = "C:\\Users\\Alice\\Documents\\big2.bin"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def ingest_fixture(tmp_path: Path) -> Path:
    """Ingest the committed candidate fixture and return the index path."""
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / "candidates.csv", db_path)
    return db_path


def listing_for(db_path: Path, **kwargs: object) -> opportunities.OpportunityList:
    """Build the list with the test defaults."""
    options: dict[str, object] = {
        "min_size": MIN,
        "list_top": 50,
        "explicit": 50,
        "now": NOW,
        "db_path": str(db_path),
    }
    options.update(kwargs)
    conn = db.open_db(db_path)
    try:
        return build_opportunities(conn, rules.load_rules(), **options)  # type: ignore[arg-type]
    finally:
        conn.close()


def row_for(listing: opportunities.OpportunityList, path: str) -> Opportunity:
    """The listed row with ``path`` (fails loudly when it is missing)."""
    found = listing.row(path)
    assert found is not None, f"{path!r} is not on the list"
    return found


def sub_rows(listing: opportunities.OpportunityList, prefix: str) -> list[Opportunity]:
    """Rows below a folder, by path prefix."""
    return [row for row in listing.rows if row.path.startswith(prefix + "\\")]


def make_row(
    path: str,
    *,
    size: int = MIN,
    gain: int | None = None,
    state: str = STATE_ACTION,
    is_dir: bool = False,
    action: str = "DELETE_QUARANTINE",
    kind: str | None = "delete",
    tier: str = "T1",
    confidence: float = 0.9,
    category: str = "dev-cache",
    why: str = "Delete (quarantine): a cache.",
    score: float = 1.0,
    advisory: bool = False,
) -> Opportunity:
    """A hand-built row for the pure-arithmetic tests."""
    return Opportunity(
        key=opportunities.path_key(path),
        path=path,
        is_dir=is_dir,
        size=size,
        gain=size if gain is None else gain,
        gain_basis=opportunities.GAIN_FULL,
        state=state,
        action=action,
        tier=tier,
        confidence=confidence,
        category=category,
        rationale="a cache",
        why=why,
        score=score,
        kind=kind,
        advisory=advisory,
    )


# --------------------------------------------------------------------------- #
# Rows from the engine
# --------------------------------------------------------------------------- #


def test_list_is_sorted_by_gain_descending(tmp_path: Path) -> None:
    """The screen's promise: biggest estimated gain first."""
    listing = listing_for(ingest_fixture(tmp_path))
    gains = [row.gain for row in listing.rows]
    assert gains == sorted(gains, reverse=True)
    assert listing.heading().startswith(f"{len(listing.rows)} opportunities")


def test_every_state_is_present_and_no_action_rows_are_never_hidden(tmp_path: Path) -> None:
    """Has action / no action / undecided are all on the list by default."""
    listing = listing_for(ingest_fixture(tmp_path))
    summary = listing.summary
    assert summary.actionable > 0
    assert summary.no_action > 0
    assert summary.undecided > 0

    no_action = [row for row in listing.rows if row.state == STATE_NO_ACTION]
    assert {row.path for row in no_action} >= {WINDOWS, WIDGET}
    for row in no_action:
        assert row.solution == "No action"
        assert row.why.startswith("No action: ")
        assert row.gain == 0 and row.gain_label == "--"
        assert row.rationale.strip()


def test_rule_based_solutions_come_from_the_classifier(tmp_path: Path) -> None:
    """Every row's solution is the engine's verdict, verbatim."""
    listing = listing_for(ingest_fixture(tmp_path))
    cache = row_for(listing, CHROME_CACHE)
    assert cache.action == "DELETE_QUARANTINE"
    assert cache.state == STATE_ACTION
    assert cache.category == "browser-cache"
    assert cache.rule_id is not None
    assert cache.why.startswith("Delete (quarantine): Chrome resource cache")
    assert cache.gain == cache.size and cache.gain_basis == opportunities.GAIN_FULL

    videos = row_for(listing, VIDEOS)
    assert videos.is_dir and videos.action == "MOVE" and videos.state == STATE_ACTION
    assert videos.kind == "move"

    unknown = row_for(listing, UNKNOWN_FILE)
    assert unknown.state == STATE_UNDECIDED
    assert unknown.tier == "T3" and unknown.rule_id is None
    assert unknown.why.startswith("Review: No rule matched")
    assert unknown.gain == unknown.size  # upper bound: nothing is freed yet
    assert unknown.gain_label.startswith("up to ")


def test_folder_rows_aggregate_their_descendants(tmp_path: Path) -> None:
    """A folder row is its file-row subtree, the same arithmetic as the engine."""
    db_path = ingest_fixture(tmp_path)
    listing = listing_for(db_path)
    conn = db.open_db(db_path)
    try:
        expected = int(
            conn.execute(
                "SELECT COALESCE(SUM(size), 0) FROM entries WHERE is_dir = 0 AND path LIKE ?",
                (VIDEOS + "\\%",),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert row_for(listing, VIDEOS).size == expected
    # The children are listed too, and their sizes add up to the folder's.
    children = sub_rows(listing, VIDEOS)
    assert children, "the fixture lists files inside the Videos folder"
    assert sum(child.size for child in children) <= row_for(listing, VIDEOS).size


def test_scan_rows_fill_in_what_the_candidates_did_not_cover(tmp_path: Path) -> None:
    """The biggest unmatched and KEEP entries make the list honest."""
    listing = listing_for(ingest_fixture(tmp_path))
    assert listing.explicit_added > 0
    scan_rows = [row for row in listing.rows if row.explicit_scan]
    assert scan_rows, "the bounded scan contributed rows"
    for row in scan_rows:
        assert row.path and row.why
        # A scan row must not repeat another listed row's numbers: it is either
        # an explicit *No action* (frees nothing) or holds nothing else listed.
        if row.gain > 0 and row.is_dir:
            assert not sub_rows(listing, row.path), f"{row.path} repeats its children"


def test_duplicate_clusters_are_advisory_and_unverified(tmp_path: Path) -> None:
    """A name+size cluster is a review item until the deep scan proves it."""
    listing = listing_for(ingest_fixture(tmp_path))
    cluster = [row for row in listing.rows if row.kind == "dupes-weak"]
    assert cluster, "the fixture has a weak duplicate cluster"
    for row in cluster:
        assert row.state == STATE_UNDECIDED
        assert row.weak and row.advisory
        assert row.action == "REVIEW"
        note = advisory_note(row)
        assert note is not None and "unverified" in note
        assert row.member_bytes > row.gain  # gain is the recoverable copies only


def test_summary_deduplicates_nested_rows(tmp_path: Path) -> None:
    """The strip's totals count a branch once, however many rows describe it."""
    listing = listing_for(ingest_fixture(tmp_path))
    summary = listing.summary
    top = top_level_rows(listing.rows)
    assert summary.effective_rows == len(top) < len(listing.rows)
    assert summary.gain_bytes == sum(row.gain for row in top)
    assert summary.size_bytes == sum(row.size for row in top)
    assert summary.actionable + summary.no_action + summary.undecided == summary.rows
    assert summary.state_total(STATE_ACTION).gain >= summary.state_total(STATE_NO_ACTION).gain
    volume_gains = [item.gain for item in summary.volumes]
    assert volume_gains == sorted(volume_gains, reverse=True)
    assert {item.volume for item in summary.volumes} == {row.volume for row in listing.rows}
    assert summary.categories[0].rows >= 1
    assert {item.kind for item in summary.kinds} <= set(opportunities.KIND_LABELS)


def test_list_document_round_trips(tmp_path: Path) -> None:
    """The JSON twin carries everything the rows need (and reloads)."""
    listing = listing_for(ingest_fixture(tmp_path))
    document = listing.to_dict()
    assert document["schema"] == opportunities.SCHEMA
    payload = json.loads(json.dumps(document))
    reborn = rows_from_dicts(payload["items"])
    assert len(reborn) == len(listing.rows)
    assert reborn[0].path == listing.rows[0].path
    assert reborn[0].gain == listing.rows[0].gain


def test_build_refuses_negative_limits(tmp_path: Path) -> None:
    """Bad arguments fail loudly instead of silently listing nothing."""
    db_path = ingest_fixture(tmp_path)
    conn = db.open_db(db_path)
    try:
        with pytest.raises(OpportunitiesError):
            build_opportunities(conn, rules.load_rules(), min_size=-1)
        with pytest.raises(OpportunitiesError):
            build_opportunities(conn, rules.load_rules(), list_top=-1)
        with pytest.raises(OpportunitiesError):
            build_opportunities(conn, rules.load_rules(), explicit=-1)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Selection: the folder cascade
# --------------------------------------------------------------------------- #


def cascade_rows() -> tuple[Opportunity, ...]:
    """A tiny tree: folder (1 GiB) with two children, one of them a folder."""
    return (
        make_row("C:\\data\\big", size=1024 * MIB, is_dir=True),
        make_row("C:\\data\\big\\a.bin", size=700 * MIB),
        make_row("C:\\data\\big\\sub", size=300 * MIB, is_dir=True),
        make_row("C:\\data\\big\\sub\\b.bin", size=300 * MIB),
        make_row("C:\\other.bin", size=10 * MIB),
    )


def test_selecting_a_folder_covers_its_descendants() -> None:
    """A folder counts once, its children render as covered."""
    selection = Selection(cascade_rows())
    selection.toggle("c:\\data\\big")
    assert selection.gain == 1024 * MIB  # not + 700 MiB + 300 MiB
    assert selection.is_selected("c:\\data\\big")
    for key in ("c:\\data\\big\\a.bin", "c:\\data\\big\\sub", "c:\\data\\big\\sub\\b.bin"):
        assert not selection.is_selected(key)
        assert selection.is_covered(key)
        assert selection.state(key) == "covered"
    assert selection.state("c:\\other.bin") == "unchecked"


def test_selecting_a_covered_row_moves_the_selection_down() -> None:
    """Clicking a child says "this one": the covering folder is released."""
    selection = Selection(cascade_rows())
    selection.toggle("c:\\data\\big")
    selection.toggle("c:\\data\\big\\a.bin")
    assert selection.keys == ("c:\\data\\big\\a.bin",)
    assert selection.gain == 700 * MIB
    assert not selection.is_covered("c:\\data\\big\\a.bin")
    assert selection.state("c:\\data\\big") == "partial"
    assert selection.has_selected_descendant("c:\\data\\big")


def test_unchecking_releases_what_it_covered() -> None:
    """Unchecking a folder puts its children back on the table."""
    selection = Selection(cascade_rows())
    selection.toggle("c:\\data\\big")
    selection.toggle("c:\\data\\big")
    assert len(selection) == 0 and selection.gain == 0
    assert selection.state("c:\\data\\big\\a.bin") == "unchecked"
    selection.toggle("c:\\data\\big\\a.bin")
    assert selection.keys == ("c:\\data\\big\\a.bin",)


def test_select_all_picks_folders_first_and_never_double_counts() -> None:
    """Bulk select walks the tree top-down; covered children stay out."""
    selection = Selection(cascade_rows())
    selection.select_all()
    assert selection.keys == ("c:\\data\\big", "c:\\other.bin")
    assert selection.gain == 1024 * MIB + 10 * MIB
    selection.clear()
    assert len(selection) == 0 and selection.keys == ()


def test_select_all_respects_a_visible_subset() -> None:
    """The filter bar's "select all" only checks what the user can see."""
    selection = Selection(cascade_rows())
    selection.select_all(["c:\\data\\big\\sub", "c:\\other.bin"])
    assert selection.keys == ("c:\\data\\big\\sub", "c:\\other.bin")
    assert selection.gain == 300 * MIB + 10 * MIB


def test_toggle_ignores_unknown_keys() -> None:
    """Rows that vanished from the list cannot be selected."""
    selection = Selection(cascade_rows())
    assert selection.toggle("c:\\not-listed") == ()
    assert len(selection) == 0


# --------------------------------------------------------------------------- #
# Filters, sorting, options
# --------------------------------------------------------------------------- #


def test_filters_select_by_size_state_tier_and_category(tmp_path: Path) -> None:
    """Each filter control maps to one predicate, and they combine."""
    listing = listing_for(ingest_fixture(tmp_path))
    rows = listing.rows

    assert len(apply_filter(rows, OpportunityFilter())) == len(rows)
    big = apply_filter(rows, OpportunityFilter(min_size=500 * MIB))
    assert big and all(row.size >= 500 * MIB for row in big)

    no_action = apply_filter(rows, OpportunityFilter(state=STATE_NO_ACTION))
    assert {row.path for row in no_action} >= {WINDOWS, WIDGET}
    undecided = apply_filter(rows, OpportunityFilter(state=STATE_UNDECIDED))
    assert undecided and all(row.state == STATE_UNDECIDED for row in undecided)

    t3 = apply_filter(rows, OpportunityFilter(tier="T3"))
    assert t3 and all(row.tier == "T3" for row in t3)

    category = opportunities.option_values(rows, "category")[0]
    by_category = apply_filter(rows, OpportunityFilter(category=category))
    assert by_category and all(row.category == category for row in by_category)
    assert opportunities.option_values(rows, "state") == (
        STATE_ACTION,
        STATE_NO_ACTION,
        STATE_UNDECIDED,
    )


def test_search_matches_paths_solutions_and_reasons(tmp_path: Path) -> None:
    """The search box looks at the path, the solution and the reason."""
    listing = listing_for(ingest_fixture(tmp_path))
    by_path = apply_filter(listing.rows, OpportunityFilter(text="holiday.mp4"))
    assert [row.path for row in by_path] == [VIDEOS_CLIP]
    by_solution = apply_filter(listing.rows, OpportunityFilter(text="quarantine"))
    assert by_solution and all("quarantine" in row.solution.casefold() for row in by_solution)
    by_reason = apply_filter(listing.rows, OpportunityFilter(text="CHROME RESOURCE CACHE"))
    assert by_reason and all("chrome resource cache" in row.why.casefold() for row in by_reason)
    assert apply_filter(listing.rows, OpportunityFilter(text="nothing-matches-this")) == ()


def test_sort_columns_are_deterministic() -> None:
    """Header clicks order by one column with a stable path tiebreak."""
    rows = cascade_rows()
    assert [row.gain for row in sort_rows(rows, "gain")] == sorted(
        (row.gain for row in rows), reverse=True
    )
    assert [row.size for row in sort_rows(rows, "size", descending=False)] == sorted(
        row.size for row in rows
    )
    assert [row.path for row in sort_rows(rows, "path", descending=False)] == sorted(
        row.path for row in rows
    )
    by_tier = sort_rows(rows, "tier")
    assert [row.tier for row in by_tier] == sorted((row.tier for row in rows), reverse=True)
    with pytest.raises(OpportunitiesError):
        sort_rows(rows, "nonsense")


def test_summarise_counts_each_branch_once() -> None:
    """A hand-built tree pins the dedup arithmetic exactly."""
    rows = cascade_rows()
    summary = summarise(rows)
    assert summary.rows == 5 and summary.files == 3 and summary.dirs == 2
    assert summary.effective_rows == 2
    assert summary.gain_bytes == 1024 * MIB + 10 * MIB
    assert summary.volumes[0].volume == "c:" and summary.volumes[0].gain == 1034 * MIB


# --------------------------------------------------------------------------- #
# Details pane helpers
# --------------------------------------------------------------------------- #


def test_alternatives_cover_the_design_vocabulary_and_respect_the_engine(tmp_path: Path) -> None:
    """Every option the pane shows is one the engine would accept."""
    listing = listing_for(ingest_fixture(tmp_path))
    cache = row_for(listing, CHROME_CACHE)

    without_target = alternatives(cache, platform="linux")
    actions = [item.action for item in without_target]
    assert actions[0] == "DELETE_QUARANTINE"
    assert {"MOVE", "COMPRESS_NTFS", "REVIEW"} <= set(actions)
    assert ("NATIVE" in actions) == (cache.native is not None)
    move = next(item for item in without_target if item.action == "MOVE")
    assert not move.available and "target drive" in move.reason
    compress = next(item for item in without_target if item.action == "COMPRESS_NTFS")
    assert not compress.available and compress.reason == "NTFS compression needs Windows"

    with_target = alternatives(cache, target="D:", platform="win32")
    move = next(item for item in with_target if item.action == "MOVE")
    assert move.available and move.label == "Move to D:"
    assert "D:\\Moved\\Users\\Alice" in move.note

    # Compression is for files on Windows; folders are moved, never compressed.
    dump = row_for(listing, BIG_DMP)
    compress = next(
        item for item in alternatives(dump, platform="win32") if item.action == "COMPRESS_NTFS"
    )
    assert compress.available and compress.gain == dump.size
    packed_folder = next(
        item for item in alternatives(cache, platform="win32") if item.action == "COMPRESS_NTFS"
    )
    assert not packed_folder.available and "folders are moved" in packed_folder.reason

    # The same target on the entry's own volume frees nothing: refused, with why.
    same_volume = alternatives(cache, target="C:", platform="win32")
    move = next(item for item in same_volume if item.action == "MOVE")
    assert not move.available and "same volume" in move.reason

    # T3 entries are report-only: even delete is off the table.
    unknown = row_for(listing, UNKNOWN_FILE)
    delete = next(
        item
        for item in alternatives(unknown, platform="linux")
        if item.action == "DELETE_QUARANTINE"
    )
    assert not delete.available and "T3" in delete.reason


def test_alternatives_offer_the_vendor_command_when_the_rules_name_one(tmp_path: Path) -> None:
    """A native command rides along as its own alternative."""
    listing = listing_for(ingest_fixture(tmp_path))
    steam = row_for(listing, "D:\\Games\\SteamLibrary")
    labels = [item.label for item in alternatives(steam, platform="win32")]
    assert "Use the native tool" in labels
    native = next(item for item in alternatives(steam, platform="win32") if item.action == "NATIVE")
    assert steam.native and steam.native in native.note


def test_side_effects_and_advisory_notes_match_the_plan(tmp_path: Path) -> None:
    """The pane's wording is the plan's wording, not a second vocabulary."""
    listing = listing_for(ingest_fixture(tmp_path))
    cache = row_for(listing, CHROME_CACHE)
    assert side_effects(cache).startswith("Quarantined, never erased:")
    assert advisory_note(cache) is None

    app_row = row_for(listing, "C:\\Users\\Alice\\AppData\\Local\\Google")
    assert app_row.advisory and app_row.state == STATE_UNDECIDED
    note = advisory_note(app_row)
    assert note is not None and "whole application footprint" in note

    windows = row_for(listing, WINDOWS)
    assert (
        side_effects(windows) == "Nothing happens: the rules say this entry should be left alone."
    )

    videos = row_for(listing, VIDEOS)
    assert "target drive you pick" in side_effects(videos)
    moved = side_effects(videos, target="D:")
    assert moved.startswith("Moved to D:\\Moved\\Users\\Alice\\Videos and linked back with a ")
    assert "original path keeps resolving" in moved
    clip = row_for(listing, VIDEOS_CLIP)
    assert "elevation" in side_effects(clip, target="D:")


def test_destination_and_link_come_from_the_planner(tmp_path: Path) -> None:
    """The destination editor's default is the plan's own arithmetic."""
    listing = listing_for(ingest_fixture(tmp_path))
    videos = row_for(listing, VIDEOS)
    assert destination_for(videos, "D:") == "D:\\Moved\\Users\\Alice\\Videos"
    assert link_for(videos, "D:") == ("JUNCTION", False)
    clip = row_for(listing, VIDEOS_CLIP)
    assert link_for(clip, "D:") == ("SYMLINK", True)
    assert link_for(clip, "C:") == ("HARDLINK", False)
    assert destination_for(row_for(listing, WINDOWS), "D:") == "D:\\Moved\\Windows"


def test_rows_report_their_volume() -> None:
    """Volumes group the summary strip: Windows drives and POSIX roots alike."""
    assert make_row("C:\\a\\b").volume == "c:"
    assert make_row("D:\\x").volume == "d:"
    assert make_row("\\\\server\\share\\file").volume == "\\\\server\\share"
    assert make_row("/home/user/file").volume == "/home"


def test_engine_helpers_used_by_the_list_are_public() -> None:
    """The list leans on planner/engine helpers instead of copying them."""
    from spacesage import planner

    # volume_of is the public face of the same-volume arithmetic the planner uses.
    assert planner.volume_of("D:\\x") == planner.volume_of("D:\\y")
    assert not planner.same_volume("D:\\x", "C:\\")
    assert planner.side_effects_of("MOVE") is None
    assert planner.side_effects_of("KEEP") is None
    assert row_for.__module__ == "test_opportunities"
