"""Rule-pack tests: matcher semantics, validation, loading, classification, CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spacesage import db, rules
from spacesage.ingest import ingest_csv
from spacesage.rules import (
    UNKNOWN_ACTION,
    UNKNOWN_CATEGORY,
    UNKNOWN_TIER,
    EntryFacts,
    RulesError,
    RuleSet,
)

DATA_DIR = Path(__file__).resolve().parent / "fixtures" / "data"

#: Fixed reference point for age-based rules (2026-09-12 12:00:00 UTC).
NOW = int(datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC).timestamp())
DAY = 86_400


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def pack_text(*rule_blocks: str, pack: str | None = None) -> str:
    """Assemble a pack document from ``[[rule]]`` blocks."""
    header = pack if pack is not None else '[pack]\nid = "test"\n'
    return header + "\n" + "\n".join(rule_blocks)


def load_text(text: str, *, user: bool = False) -> RuleSet:
    """Parse one pack document into a rule set (no filesystem involved)."""
    parsed = rules.parse_pack(text, source="<test>", default_id="test", user=user)
    return RuleSet.from_rules(parsed.rules, builtin_packs=(parsed.id,))


def facts(
    path: str,
    *,
    size: int = 0,
    is_dir: bool = False,
    ext: str | None = None,
    name: str | None = None,
    mtime: int | None = None,
) -> EntryFacts:
    """Build matcher facts; ``name`` and ``ext`` default from ``path``."""
    resolved_name = name if name is not None else path.replace("/", "\\").rsplit("\\", 1)[-1]
    if ext is None and not is_dir and "." in resolved_name:
        ext = resolved_name.rsplit(".", 1)[-1].lower()
    return EntryFacts(path=path, name=resolved_name, is_dir=is_dir, size=size, ext=ext, mtime=mtime)


def match_of(text: str, entry: EntryFacts, *, now: float = NOW) -> rules.Rule | None:
    """The rule one entry matches in a pack document (``None`` = unknown)."""
    return load_text(text).match(entry, now=now)


def ingest_fixture(tmp_path: Path, name: str = "rule_packs.csv") -> Path:
    """Ingest a committed fixture and return the index path."""
    db_path = tmp_path / "index.db"
    ingest_csv(DATA_DIR / name, db_path)
    return db_path


def class_map(db_path: Path, ruleset: RuleSet | None = None) -> dict[str, rules.Classification]:
    """Classification per path for an index."""
    conn = db.open_db(db_path)
    try:
        effective = ruleset if ruleset is not None else rules.load_rules()
        return {item.path: item for item in rules.iter_classifications(conn, effective, now=NOW)}
    finally:
        conn.close()


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "spacesage", *args],
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------- #
# Glob semantics
# --------------------------------------------------------------------------- #


def test_glob_star_does_not_cross_separators() -> None:
    text = pack_text(
        """
[[rule]]
id = "one-level"
path = ["**/Temp/*.tmp"]
category = "temp-files"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\Windows\\Temp\\a.tmp")) is not None
    assert match_of(text, facts("C:\\Windows\\Temp\\sub\\a.tmp")) is None
    assert match_of(text, facts("C:\\Windows\\Temp\\a.txt")) is None


def test_glob_double_star_crosses_separators() -> None:
    one = facts("C:\\Windows\\Temp")
    text = pack_text(
        """
[[rule]]
id = "subtree"
path = ["**/Windows/Temp/**"]
category = "windows-temp"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, one) is not None  # the folder itself
    assert match_of(text, facts("C:\\Windows\\Temp\\deep\\down\\x.bin")) is not None
    assert match_of(text, facts("C:\\Windows\\Temperature\\x.bin")) is None
    assert match_of(text, facts("C:\\Temp\\Windows\\x")) is None


def test_glob_middle_double_star_matches_zero_or_more_segments() -> None:
    text = pack_text(
        """
[[rule]]
id = "middle"
path = ["**/a/**/b"]
category = "test"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\a\\b")) is not None
    assert match_of(text, facts("C:\\x\\a\\b")) is not None
    assert match_of(text, facts("C:\\a\\x\\y\\b")) is not None
    assert match_of(text, facts("C:\\a\\x\\y\\bc")) is None


def test_glob_separator_and_case_rules() -> None:
    text = pack_text(
        """
[[rule]]
id = "case"
path = ["**/Cache/**"]
category = "cache"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    # Windows paths match case-insensitively and accept either separator
    assert match_of(text, facts("c:\\users\\a\\appdata\\local\\CACHE\\x")) is not None
    assert match_of(text, facts("C:/Users/a/AppData/Local/cache/x")) is not None
    # POSIX paths stay case-sensitive
    assert match_of(text, facts("/home/a/Downloads/Cache/x")) is not None
    assert match_of(text, facts("/home/a/.cache/x")) is None


def test_glob_question_mark_and_class() -> None:
    text = pack_text(
        """
[[rule]]
id = "chars"
path = ["**/log[0-9].txt", "**/log?.txt"]
category = "logs"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\x\\log1.txt")) is not None
    assert match_of(text, facts("C:\\x\\log7.txt")) is not None
    assert match_of(text, facts("C:\\x\\log.txt")) is None
    assert match_of(text, facts("C:\\x\\logs\\log1.txt")) is not None


def test_drive_root_and_unc_paths_are_windows_style() -> None:
    assert rules._windows_style("C:\\")
    assert rules._windows_style("D:\\Users")
    assert rules._windows_style("\\\\server\\share\\x")
    assert not rules._windows_style("/home/user/x")
    assert not rules._windows_style("relative\\path")


def test_glob_to_regex_is_anchored_and_escapes_literals() -> None:
    assert rules.glob_to_regex("**/hiberfil.sys").endswith(r"hiberfil\.sys$")
    assert rules.glob_to_regex("C:\\Windows\\**").startswith("^C:")
    assert rules.glob_to_regex("**/$Recycle.Bin/**").startswith("^(?:.*[\\\\/])?")


# --------------------------------------------------------------------------- #
# Matchers
# --------------------------------------------------------------------------- #


def test_ext_matcher_ignores_the_dot_and_never_matches_folders() -> None:
    text = pack_text(
        """
[[rule]]
id = "logs"
ext = [".LOG", "bak"]
category = "logs"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\x\\app.log")) is not None
    assert match_of(text, facts("C:\\x\\app.LOG")) is not None
    assert match_of(text, facts("C:\\x\\old.bak")) is not None
    assert match_of(text, facts("C:\\x\\app.txt")) is None
    assert match_of(text, facts("C:\\x\\logs", is_dir=True)) is None


def test_min_size_matcher_uses_the_facts_size() -> None:
    text = pack_text(
        """
[[rule]]
id = "big"
min_size = "1 MiB"
category = "big"
tier = "T3"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\x\\a.bin", size=1024 * 1024)) is not None
    assert match_of(text, facts("C:\\x\\a.bin", size=1024 * 1024 - 1)) is None


def test_older_than_days_needs_a_known_timestamp() -> None:
    text = pack_text(
        """
[[rule]]
id = "old"
older_than_days = 30
category = "old"
tier = "T2"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\x\\a.bin", mtime=NOW - 31 * DAY)) is not None
    assert match_of(text, facts("C:\\x\\a.bin", mtime=NOW - 30 * DAY)) is not None
    assert match_of(text, facts("C:\\x\\a.bin", mtime=NOW - 29 * DAY)) is None
    assert match_of(text, facts("C:\\x\\a.bin", mtime=None)) is None
    assert match_of(text, facts("C:\\x\\a.bin", mtime=NOW + DAY)) is None


def test_name_regex_searches_the_last_component() -> None:
    text = pack_text(
        """
[[rule]]
id = "office-locks"
name_regex = "^~\\\\$"
category = "temp-files"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(text, facts("C:\\x\\~$report.docx")) is not None
    assert match_of(text, facts("C:\\x\\report.docx")) is None
    # Windows filenames are case-insensitive, POSIX ones are not
    assert match_of(text, facts("C:\\x\\~$Report.docx")) is not None
    posix = pack_text(
        """
[[rule]]
id = "python"
name_regex = "\\\\.py$"
category = "python"
tier = "T3"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    assert match_of(posix, facts("/srv/app/main.py")) is not None
    assert match_of(posix, facts("/srv/app/main.PY")) is None


def test_matcher_kinds_are_anded_and_lists_ored() -> None:
    text = pack_text(
        """
[[rule]]
id = "combo"
path = ["**/Downloads/**", "**/Temp/**"]
ext = ["exe", "msi"]
min_size = "1 MiB"
category = "installer"
tier = "T2"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    big = 5 * 1024 * 1024
    small = 1024 * 1024 - 1
    assert match_of(text, facts("C:\\Users\\a\\Downloads\\setup.exe", size=big)) is not None
    assert match_of(text, facts("C:\\Temp\\setup.msi", size=big)) is not None
    assert match_of(text, facts("C:\\Users\\a\\Downloads\\setup.exe", size=small)) is None
    assert match_of(text, facts("C:\\Users\\a\\Downloads\\notes.txt", size=big)) is None
    assert match_of(text, facts("C:\\other\\setup.exe", size=big)) is None


def test_first_matching_rule_wins() -> None:
    text = pack_text(
        """
[[rule]]
id = "specific"
path = ["**/AppData/Local/Temp/**"]
category = "user-temp"
tier = "T1"
action = "DELETE_QUARANTINE"
confidence = 0.9
rationale = "test"

[[rule]]
id = "broad"
path = ["**/AppData/**"]
category = "appdata"
tier = "T3"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    )
    match = match_of(text, facts("C:\\Users\\a\\AppData\\Local\\Temp\\x.tmp"))
    assert match is not None and match.id == "specific"
    broad = match_of(text, facts("C:\\Users\\a\\AppData\\Local\\other\\x.bin"))
    assert broad is not None and broad.id == "broad"
    assert match_of(text, facts("C:\\Users\\a\\Documents\\x.bin")) is None


def test_unmatched_entries_fall_back_to_unknown() -> None:
    ruleset = load_text(
        pack_text(
            """
[[rule]]
id = "narrow"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "DELETE_QUARANTINE"
confidence = 0.9
rationale = "test"
"""
        )
    )
    verdict = ruleset.classify(facts("C:\\Users\\a\\Documents\\thesis.docx"), now=NOW)
    assert (verdict.category, verdict.tier, verdict.action) == (
        UNKNOWN_CATEGORY,
        UNKNOWN_TIER,
        UNKNOWN_ACTION,
    )
    assert verdict.confidence == 0.0
    assert verdict.rule_id is None and verdict.pack is None
    assert "review" in verdict.rationale.lower()


# --------------------------------------------------------------------------- #
# Sizes and ids
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1024, 1024),
        ("10 MiB", 10 * 1024**2),
        ("10MiB", 10 * 1024**2),
        ("500 KB", 500 * 1024),
        ("1.5G", int(1.5 * 1024**3)),
        ("2 TiB", 2 * 1024**4),
        ("123 B", 123),
    ],
)
def test_parse_size_accepts_bytes_and_binary_suffixes(value: object, expected: int) -> None:
    assert rules.parse_size(value) == expected


@pytest.mark.parametrize("value", ["10 XB", "abc", "", True])
def test_parse_size_rejects_junk(value: object) -> None:
    with pytest.raises(RulesError, match="size"):
        rules.parse_size(value)


# --------------------------------------------------------------------------- #
# Validation (actionable errors)
# --------------------------------------------------------------------------- #


def parse_error(text: str, match: str) -> str:
    with pytest.raises(RulesError) as info:
        rules.parse_pack(text, source="packs/test.toml", default_id="test")
    message = str(info.value)
    assert match in message, message
    assert "packs/test.toml" in message
    return message


def test_validation_reports_the_file_and_missing_keys() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "incomplete"
path = ["**/Temp/**"]
category = "temp-files"
"""
        ),
        "missing required key(s): tier, action, confidence, rationale",
    )


def test_validation_rejects_unknown_keys() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "typo"
pat = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "DELETE_QUARANTINE"
confidence = 0.9
rationale = "test"
"""
        ),
        "unknown key(s) pat",
    )


def test_validation_rejects_unknown_pack_keys_and_tables() -> None:
    parse_error('[pack]\nid = "test"\nowner = "me"\n', "unknown [pack] key(s) owner")
    parse_error("[other]\nkey = 1\n", "unknown table(s) other")


def test_validation_rejects_bad_tier_action_and_confidence() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "tier"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T4"
action = "DELETE_QUARANTINE"
confidence = 0.9
rationale = "test"
"""
        ),
        "unknown tier 'T4'; use one of T1, T2, T3",
    )
    parse_error(
        pack_text(
            """
[[rule]]
id = "action"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "DELETE"
confidence = 0.9
rationale = "test"
"""
        ),
        "unknown action 'DELETE'",
    )
    parse_error(
        pack_text(
            """
[[rule]]
id = "confidence"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "DELETE_QUARANTINE"
confidence = 1.5
rationale = "test"
"""
        ),
        "between 0 and 1",
    )


def test_validation_requires_a_native_command_for_native_actions() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "no-command"
path = ["**/WinSxS/**"]
category = "windows-component-store"
tier = "T3"
action = "NATIVE"
confidence = 0.5
rationale = "test"
"""
        ),
        "action 'NATIVE' needs a 'native' command",
    )


def test_validation_requires_at_least_one_matcher() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "empty"
category = "everything"
tier = "T3"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
        ),
        "no matcher",
    )


def test_validation_reports_bad_regex_and_sizes() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "bad-regex"
name_regex = "(["
category = "test"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
        ),
        "invalid 'name_regex'",
    )
    parse_error(
        pack_text(
            """
[[rule]]
id = "bad-size"
min_size = "10 XB"
category = "test"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
        ),
        "unknown size unit 'XB'",
    )


def test_validation_rejects_trailing_separators_and_empty_packs() -> None:
    parse_error(
        pack_text(
            """
[[rule]]
id = "slash"
path = ["**/Temp/"]
category = "temp-files"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
        ),
        "must not end with a separator",
    )
    parse_error('[pack]\nid = "test"\n', "defines no [[rule]] entries")


def test_validation_rejects_duplicate_ids_in_one_pack() -> None:
    block = """
[[rule]]
id = "dupe"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    parse_error(pack_text(block, block), "duplicate rule id(s) in one pack: dupe")


def test_validation_rejects_invalid_toml_and_ids() -> None:
    parse_error("[[rule]\nid = ", "invalid TOML")
    parse_error(
        pack_text(
            """
[[rule]]
id = "Not Lowercase"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
        ),
        "rule id must be lowercase",
    )


# --------------------------------------------------------------------------- #
# Built-in packs
# --------------------------------------------------------------------------- #


def test_builtin_packs_load_validate_and_have_unique_ids() -> None:
    packs = [rules.load_pack(path) for path in sorted(rules.BUILTIN_RULES_DIR.glob("*.toml"))]
    names = {path.stem for path in rules.BUILTIN_RULES_DIR.glob("*.toml")}
    assert names == {
        "windows",
        "dev",
        "browsers",
        "media",
        "games",
        "installers",
        "misc",
    }
    assert [pack.id for pack in packs] == [
        "browsers",
        "dev",
        "games",
        "installers",
        "media",
        "misc",
        "windows",
    ]
    ids = [rule.id for pack in packs for rule in pack.rules]
    assert len(ids) == len(set(ids))
    for pack in packs:
        assert pack.title and pack.source.endswith(".toml")
        for rule in pack.rules:
            assert rule.tier in rules.TIERS
            assert rule.action in rules.ACTIONS
            assert 0.0 <= rule.confidence <= 1.0
            assert len(rule.rationale) >= 20
            assert rule.paths or rule.exts or rule.min_size is not None or rule.name_pattern
            if rule.action == "NATIVE":
                assert rule.native
    assert len(ids) >= 60


def test_builtin_pack_order_is_deterministic_and_documented() -> None:
    ruleset = rules.load_rules()
    assert ruleset.builtin_packs == (
        "browsers",
        "dev",
        "games",
        "installers",
        "media",
        "misc",
        "windows",
    )
    assert ruleset.shadowed == ()
    packs = {
        rules.load_pack(path).id: rules.load_pack(path).order
        for path in rules.BUILTIN_RULES_DIR.glob("*.toml")
    }
    assert packs["windows"] < packs["dev"] < packs["browsers"] < packs["games"]
    assert packs["games"] < packs["installers"] < packs["misc"] < packs["media"]


def test_load_rules_requires_the_builtin_directory(tmp_path: Path) -> None:
    with pytest.raises(RulesError, match="built-in rule packs are missing"):
        rules.load_rules(builtin_dir=tmp_path / "nope", include_user=False)


# --------------------------------------------------------------------------- #
# User packs: shadowing and precedence
# --------------------------------------------------------------------------- #


def write_pack(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(text, encoding="utf-8")
    return target


def test_user_rules_shadow_builtins_by_id_and_are_tried_first(tmp_path: Path) -> None:
    user_dir = tmp_path / "user"
    write_pack(
        user_dir,
        "custom.toml",
        """
[pack]
id = "custom"
title = "Custom"
order = 5

[[rule]]
id = "my-exception"
path = ["**/Documents/proj/node_modules/**"]
category = "keep-this"
tier = "T3"
action = "KEEP"
confidence = 0.9
rationale = "the dependencies in this one project must stay put"

[[rule]]
id = "pip-cache"
path = ["**/AppData/Local/pip/Cache/**"]
category = "my-pip-cache"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "my override of the built-in pip rule"
""",
    )
    ruleset = rules.load_rules(user_dir=user_dir)
    assert ruleset.user_packs == ("custom",)
    assert ruleset.shadowed == ("pip-cache",)
    assert ruleset.rules[0].id == "my-exception"  # user section first
    pip_index = next(index for index, rule in enumerate(ruleset.rules) if rule.id == "pip-cache")
    dev_index = next(index for index, rule in enumerate(ruleset.rules) if rule.id == "uv-cache")
    assert pip_index < dev_index  # the shadow keeps the user position, before built-ins

    exception = ruleset.classify(
        facts("D:\\work\\Documents\\proj\\node_modules\\react\\index.js"), now=NOW
    )
    assert exception.category == "keep-this" and exception.action == "KEEP"
    assert (
        ruleset.classify(facts("C:\\Users\\a\\AppData\\Local\\pip\\Cache\\x.whl"), now=NOW).category
        == "my-pip-cache"
    )
    # untouched built-ins still win over the unknown fallback
    assert ruleset.classify(facts("C:\\Windows\\Temp\\x.tmp"), now=NOW).rule_id == "windows-temp"


def test_duplicate_ids_across_user_packs_are_rejected(tmp_path: Path) -> None:
    user_dir = tmp_path / "user"
    block = """
[[rule]]
id = "same"
path = ["**/Temp/**"]
category = "temp-files"
tier = "T1"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
    write_pack(user_dir, "a.toml", '[pack]\nid = "a"\n' + block)
    write_pack(user_dir, "b.toml", '[pack]\nid = "b"\n' + block)
    with pytest.raises(RulesError, match="duplicate rule id\\(s\\) across user packs: same"):
        rules.load_rules(user_dir=user_dir)


def test_user_dir_resolution_honours_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(rules.RULES_ENV_VAR, str(tmp_path / "packs"))
    assert rules.default_rules_dir() == tmp_path / "packs"
    monkeypatch.delenv(rules.RULES_ENV_VAR)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    if sys.platform == "win32":
        # The Windows branch reads %APPDATA% for the same fallback.
        monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    assert rules.default_rules_dir() == tmp_path / "cfg" / "spacesage" / "rules"


def test_missing_user_directory_is_not_an_error(tmp_path: Path) -> None:
    ruleset = rules.load_rules(user_dir=tmp_path / "absent")
    assert ruleset.user_packs == ()
    assert len(ruleset.rules) >= 60


def test_no_path_rules_are_checked_for_every_entry() -> None:
    ruleset = load_text(
        pack_text(
            """
[[rule]]
id = "any-big"
min_size = "10 MiB"
category = "large-file"
tier = "T3"
action = "REVIEW"
confidence = 0.5
rationale = "test"
"""
        )
    )
    assert ruleset.always == (0,)
    assert ruleset.match(facts("C:\\anywhere\\big.bin", size=20 * 1024**2), now=NOW) is not None
    assert ruleset.match(facts("C:\\anywhere\\small.bin", size=1), now=NOW) is None


# --------------------------------------------------------------------------- #
# Classification on the synthetic fixture
# --------------------------------------------------------------------------- #

#: (path, category, tier, action) for the committed rule_packs.csv fixture.
EXPECTED: tuple[tuple[str, str, str, str], ...] = (
    ("C:\\Windows\\Temp", "windows-temp", "T1", "DELETE_QUARANTINE"),
    ("C:\\Windows\\Temp\\wtmp.tmp", "windows-temp", "T1", "DELETE_QUARANTINE"),
    ("C:\\Users\\Alice\\AppData\\Local\\Temp\\utmp.tmp", "user-temp", "T1", "DELETE_QUARANTINE"),
    ("C:\\Windows\\Minidump\\mini.dmp", "crash-dumps", "T1", "DELETE_QUARANTINE"),
    (
        "C:\\Windows\\SoftwareDistribution\\Download\\update.cab",
        "windows-update",
        "T3",
        "NATIVE",
    ),
    ("C:\\Windows\\WinSxS\\Manifests\\x.manifest", "windows-component-store", "T3", "NATIVE"),
    ("C:\\hiberfil.sys", "hibernation", "T3", "NATIVE"),
    ("C:\\pagefile.sys", "pagefile", "T3", "NATIVE"),
    ("C:\\swapfile.sys", "pagefile", "T3", "NATIVE"),
    ("C:\\$Recycle.Bin\\S-1-5-21\\$R123.exe", "recycle-bin", "T3", "NATIVE"),
    ("C:\\Program Files\\Widget\\widget.exe", "installed-app", "T3", "KEEP"),
    ("C:\\Windows\\System32\\kernel.dll", "windows-system", "T3", "KEEP"),
    (
        "C:\\Users\\Alice\\AppData\\Local\\pip\\Cache\\wheels\\x.whl",
        "dev-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    ("C:\\Users\\Alice\\AppData\\Local\\uv\\cache\\b.whl", "dev-cache", "T1", "DELETE_QUARANTINE"),
    (
        "C:\\Users\\Alice\\AppData\\Local\\npm-cache\\_cacache\\a.dat",
        "dev-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\.nuget\\packages\\newtonsoft\\lib.dll",
        "dev-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\.cargo\\registry\\cache\\serde.crate",
        "dev-cache",
        "T2",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\.gradle\\caches\\modules\\kotlin.jar",
        "dev-cache",
        "T2",
        "DELETE_QUARANTINE",
    ),
    ("C:\\Users\\Alice\\.m2\\repository\\junit\\junit.jar", "dev-cache", "T2", "DELETE_QUARANTINE"),
    (
        "C:\\Users\\Alice\\Documents\\proj\\node_modules\\react\\index.js",
        "build-artifacts",
        "T2",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\Documents\\proj\\.venv\\Lib\\site.py",
        "build-artifacts",
        "T2",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\Documents\\proj\\__pycache__\\mod.pyc",
        "build-artifacts",
        "T1",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\Documents\\proj\\dist\\bundle.js",
        "build-artifacts",
        "T2",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\.cache\\huggingface\\models\\blob.bin",
        "model-cache",
        "T2",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\.cache\\torch\\hub\\checkpoint.pt",
        "model-cache",
        "T2",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Docker\\wsl\\data\\ext4.vhdx",
        "container-storage",
        "T3",
        "NATIVE",
    ),
    ("C:\\Users\\Alice\\AppData\\Local\\Docker\\wsl", "container-storage", "T3", "NATIVE"),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Google\\Chrome\\User Data\\Default\\Cache\\f_000001",
        "browser-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Microsoft\\Edge\\User Data\\Default\\Code Cache\\js.bin",
        "browser-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    (
        "C:\\Users\\Alice\\AppData\\Local\\Mozilla\\Firefox\\Profiles\\abc.default\\cache2\\entries\\e_000001",
        "browser-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    ("C:\\Users\\Alice\\Pictures\\raw\\shot.cr2", "media-photos", "T2", "MOVE"),
    ("C:\\Users\\Alice\\Pictures", "media-library", "T2", "MOVE"),
    ("C:\\Users\\Alice\\Videos\\holiday.mp4", "media-video", "T2", "MOVE"),
    (
        "D:\\Games\\SteamLibrary\\steamapps\\common\\HalfLife\\hl2.exe",
        "game-library",
        "T2",
        "NATIVE",
    ),
    (
        "D:\\Games\\SteamLibrary\\steamapps\\shadercache\\half.dxcache",
        "game-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    ("D:\\Games\\Epic Games\\Fortnite\\fort.exe", "game-library", "T2", "NATIVE"),
    ("D:\\Games\\GOG Games\\Witcher\\witcher.exe", "game-library", "T2", "NATIVE"),
    ("D:\\Games\\Battle.net\\CallOfDuty\\cod.exe", "game-library", "T2", "NATIVE"),
    (
        "C:\\Users\\Alice\\AppData\\Local\\D3DSCache\\shader.bin",
        "game-cache",
        "T1",
        "DELETE_QUARANTINE",
    ),
    ("C:\\Users\\Alice\\Downloads\\old_setup.exe", "installer", "T2", "DELETE_QUARANTINE"),
    ("C:\\Users\\Alice\\Downloads\\legacy.msi", "installer", "T2", "DELETE_QUARANTINE"),
    ("C:\\Users\\Alice\\Documents\\old.docx.bak", "backup-copy", "T2", "DELETE_QUARANTINE"),
    ("C:\\Users\\Alice\\Documents\\logs\\app.log", "logs", "T1", "DELETE_QUARANTINE"),
    ("C:\\Users\\Alice\\Documents\\logs", "logs", "T2", "REVIEW"),
    ("C:\\Users\\Alice\\Documents\\scratch.tmp", "temp-files", "T1", "DELETE_QUARANTINE"),
    ("C:\\Users\\Alice\\OneDrive\\Documents\\shared.docx", "cloud-sync", "T3", "NATIVE"),
    ("C:\\Users\\Alice\\Documents\\thesis.docx", UNKNOWN_CATEGORY, "T3", "REVIEW"),
    ("C:\\Users\\Alice\\Documents\\lookup.xlsx", UNKNOWN_CATEGORY, "T3", "REVIEW"),
)


def test_classification_covers_the_required_cases(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    classified = class_map(db_path)
    missing = [path for path, *_ in EXPECTED if path not in classified]
    assert missing == []
    for path, category, tier, action in EXPECTED:
        item = classified[path]
        assert (item.category, item.tier, item.action) == (category, tier, action), path
        if category == UNKNOWN_CATEGORY:
            assert item.rule_id is None
        else:
            assert item.rule_id is not None and item.pack is not None
            assert item.rationale and item.confidence > 0.0


def test_folder_classification_uses_file_row_subtrees(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    classified = class_map(db_path)
    assert classified["C:\\"].size == 950_351_000
    assert classified["C:\\Users\\Alice\\Pictures"].size == 600_000_000
    assert classified["C:\\Users\\Alice\\Pictures"].is_dir is True
    assert classified["C:\\Users\\Alice\\Videos"].size == 6_000_000
    # a folder smaller than the media-library threshold stays unknown
    assert classified["C:\\Users\\Alice\\Videos"].category == UNKNOWN_CATEGORY
    # rules with an age filter never match folders with no export timestamp


def test_downloads_installer_rule_needs_age_and_extension(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    classified = class_map(db_path)
    assert classified["C:\\Users\\Alice\\Downloads\\old_setup.exe"].rule_id == "downloads-installer"
    assert classified["C:\\Users\\Alice\\Downloads\\notes.txt"].category == UNKNOWN_CATEGORY


def test_classifier_fixture_is_reproducible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed fixture is exactly what the generator emits (no hand edits)."""
    from fixtures import gen_rule_packs

    monkeypatch.setattr(gen_rule_packs, "DATA_DIR", tmp_path)
    gen_rule_packs.main()
    assert (tmp_path / "rule_packs.csv").read_bytes() == (DATA_DIR / "rule_packs.csv").read_bytes()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def test_report_totals_tiers_and_categories_agree(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    conn = db.open_db(db_path)
    ruleset = rules.load_rules()
    report = rules.classify_report(conn, ruleset, top=5, now=NOW, db_path=str(db_path))

    assert report.totals.entries == 138
    assert report.totals.files == 47 and report.totals.dirs == 91
    assert report.totals.matched + report.totals.unknown == report.totals.entries
    assert report.totals.matched / report.totals.entries == pytest.approx(
        report.totals.matched_ratio
    )
    assert report.totals.matched_file_bytes + report.totals.unknown_file_bytes == 1_048_351_000
    assert report.totals.matched_file_bytes == 1_048_120_000
    assert report.totals.unknown_file_bytes == 231_000

    assert [tier.tier for tier in report.tiers] == ["T1", "T2", "T3"]
    assert sum(tier.entries for tier in report.tiers) == report.totals.entries
    assert (
        sum(tier.file_bytes for tier in report.tiers)
        == report.totals.matched_file_bytes + report.totals.unknown_file_bytes
    )
    assert len(report.categories) == 5 <= report.total_categories
    assert len(report.unknown) == 5
    assert [item.size for item in report.unknown] == sorted(
        (item.size for item in report.unknown), reverse=True
    )

    payload = report.to_dict()
    assert payload["schema"] == "spacesage.classify/v1"
    assert payload["rules"]["count"] == len(ruleset.rules)
    assert payload["totals"]["unknown"] == report.totals.unknown
    assert payload["categories"]["listed"] == 5
    json.dumps(payload)  # must be JSON-serialisable
    conn.close()


def test_report_rejects_an_empty_index_and_bad_top(tmp_path: Path) -> None:
    conn = db.open_db(tmp_path / "empty.db")
    with pytest.raises(RulesError, match="empty"):
        rules.classify_report(conn, rules.load_rules())
    with pytest.raises(RulesError, match="--top"):
        rules.classify_report(conn, rules.load_rules(), top=0)
    conn.close()


def test_render_text_and_rules_listing(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    conn = db.open_db(db_path)
    report = rules.classify_report(conn, rules.load_rules(), top=10, now=NOW, db_path=str(db_path))
    text = rules.render_text(report)
    assert "index:" in text and "(schema v3)" in text
    assert "rules: 69 rules from 7 built-in packs" in text
    assert "tiers:" in text and "categories" in text and "unknown entries" in text
    assert "media-photos" in text and "DELETE_QUARANTINE" in text
    assert rules.render_json(report).startswith("{\n")
    conn.close()

    listing = rules.render_rules(rules.load_rules())
    assert "windows-temp" in listing and "pip-cache" in listing
    assert listing.count("\n") > 60


# --------------------------------------------------------------------------- #
# Materialisation (schema v3)
# --------------------------------------------------------------------------- #


def test_build_categories_targets_the_schema_v3_table(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    conn = db.open_db(db_path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION == 3
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "categories" in tables

    ruleset = rules.load_rules()
    built = rules.build_categories(conn, ruleset, now=NOW)
    assert (built.entries, built.matched) == (138, 104)
    assert built.unknown == 34
    assert built.rules_sha256 == ruleset.fingerprint
    assert db.meta_get(conn, "classify.rules_sha256") == ruleset.fingerprint
    assert db.meta_get(conn, "classify.built_entries") == "138"
    assert str(db.meta_get(conn, "classify.built_at", "")).endswith("+00:00")

    rows = conn.execute(
        "SELECT category, tier, action, COUNT(*), SUM(bytes), SUM(is_dir) FROM categories "
        "GROUP BY category, tier, action ORDER BY category"
    ).fetchall()
    assert rows  # every entry got a row
    total = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    assert total == 138
    unknown_row = conn.execute(
        "SELECT pack, rule_id, rationale FROM categories WHERE category = 'unknown' LIMIT 1"
    ).fetchone()
    assert (
        unknown_row[0] is None
        and unknown_row[1] is None
        and "review" in str(unknown_row[2]).lower()
    )

    # rebuild is idempotent and matches a fresh report
    report = rules.classify_report(conn, ruleset, top=200, now=NOW)
    again = rules.build_categories(conn, ruleset, now=NOW)
    assert again.entries == 138
    matched_files = conn.execute(
        "SELECT SUM(bytes) FROM categories WHERE is_dir = 0 AND rule_id IS NOT NULL"
    ).fetchone()[0]
    assert matched_files == report.totals.matched_file_bytes
    conn.close()


def test_categories_follow_the_entries(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    conn = db.open_db(db_path)
    rules.build_categories(conn, rules.load_rules(), now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 138

    # reloading the index (ingest --replace) must not leave stale classifications
    ingest_csv(DATA_DIR / "basic.csv", db_path, replace=True)
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
    conn.close()


def test_fingerprint_tracks_rule_changes(tmp_path: Path) -> None:
    baseline = rules.load_rules()
    user_dir = tmp_path / "user"
    write_pack(
        user_dir,
        "extra.toml",
        """
[pack]
id = "extra"

[[rule]]
id = "extra-rule"
path = ["**/Documents/**"]
category = "documents"
tier = "T3"
action = "REVIEW"
confidence = 0.5
rationale = "a user rule for the fingerprint test"
""",
    )
    changed = rules.load_rules(user_dir=user_dir)
    assert changed.fingerprint != baseline.fingerprint
    assert rules.load_rules().fingerprint == baseline.fingerprint


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_classify_prints_the_report(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    result = run_cli("classify", "--db", str(db_path))
    assert result.returncode == 0, result.stderr
    assert f"index: {db_path} (schema v3)" in result.stdout
    assert "rules: 69 rules from 7 built-in packs (35 categories)" in result.stdout
    assert (
        "entries: 138 entries classified (47 files, 91 dirs): "
        "104 matched (75.4%), 34 entries unknown"
    ) in result.stdout
    assert "  T2" in result.stdout and "DELETE_QUARANTINE" in result.stdout
    assert "unknown entries (top 20 by size)" in result.stdout

    # read-only by default: nothing was materialised
    conn = db.open_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0
    conn.close()


def test_cli_classify_json_and_materialize(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    result = run_cli("classify", "--db", str(db_path), "--json", "--top", "3", "--materialize")
    assert result.returncode == 0, result.stderr
    assert "derived: 138 categories rows (104 matched, 34 unknown)" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "spacesage.classify/v1"
    assert payload["top"] == 3
    assert payload["categories"]["listed"] == 3

    conn = db.open_db(db_path)
    rows = conn.execute("SELECT COUNT(*) FROM categories WHERE rule_id IS NOT NULL").fetchone()[0]
    assert rows == 104
    conn.close()


def test_cli_classify_lists_rules_without_an_index(tmp_path: Path) -> None:
    result = run_cli("classify", "--list-rules", "--db", str(tmp_path / "absent.db"))
    assert result.returncode == 0, result.stderr
    assert "windows-temp" in result.stdout
    assert result.stdout.index("windows-temp") < result.stdout.index("media-video")

    as_json = run_cli("classify", "--list-rules", "--json")
    assert as_json.returncode == 0
    payload = json.loads(as_json.stdout)
    assert payload["schema"] == "spacesage.rules/v1"
    assert len(payload["rules"]) == 69
    assert payload["packs"]["builtin"] == [
        "browsers",
        "dev",
        "games",
        "installers",
        "media",
        "misc",
        "windows",
    ]


def test_cli_classify_accepts_a_user_rules_directory(tmp_path: Path) -> None:
    db_path = ingest_fixture(tmp_path)
    user_dir = tmp_path / "user"
    write_pack(
        user_dir,
        "custom.toml",
        """
[pack]
id = "custom"

[[rule]]
id = "keep-docs"
path = ["**/Documents/**"]
category = "documents"
tier = "T3"
action = "KEEP"
confidence = 0.7
rationale = "everything under Documents is mine and stays put"
""",
    )
    result = run_cli("classify", "--db", str(db_path), "--rules", str(user_dir))
    assert result.returncode == 0, result.stderr
    assert "rules: 70 rules from 7 built-in packs + 1 user pack" in result.stdout
    assert "documents" in result.stdout
    assert "C:\\Users\\Alice\\Documents\\thesis.docx" not in result.stdout  # no longer unknown


def test_cli_classify_errors(tmp_path: Path) -> None:
    missing = run_cli("classify", "--db", str(tmp_path / "absent.db"))
    assert missing.returncode == 1
    assert "error: no index at" in missing.stderr

    empty = tmp_path / "empty.db"
    db.open_db(empty).close()
    result = run_cli("classify", "--db", str(empty))
    assert result.returncode == 1
    assert "error: the index is empty" in result.stderr

    bad_dir = tmp_path / "bad"
    write_pack(bad_dir, "broken.toml", "[[rule]\n")
    result = run_cli("classify", "--db", str(tmp_path / "absent.db"), "--rules", str(bad_dir))
    assert result.returncode == 1
    assert "error:" in result.stderr and "invalid TOML" in result.stderr

    bad_top = run_cli("classify", "--db", str(tmp_path / "absent.db"), "--top", "0")
    assert bad_top.returncode == 1


# --------------------------------------------------------------------------- #
# Performance (slow)
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_perf_smoke_classification(tmp_path: Path) -> None:
    """A large index classifies in one streaming pass; prints the rate."""
    from fixtures import gen

    generated = gen.generate(
        tmp_path / "perf.csv",
        gen.GenOptions(
            seed=99,
            min_files=120_000,
            files_per_dir=12,
            dirs_per_dir=4,
            min_depth=6,
            collect_entries=False,
        ),
    )
    db_path = tmp_path / "perf.db"
    ingest_csv(generated.path, db_path)
    import time as time_module

    conn = db.open_db(db_path)
    ruleset = rules.load_rules()
    started = time_module.perf_counter()
    report = rules.classify_report(conn, ruleset, top=10)
    elapsed = time_module.perf_counter() - started
    rate = report.totals.entries / elapsed
    print(
        f"\nclassify smoke: {report.totals.entries} entries in {elapsed:.2f}s "
        f"-> {rate:,.0f} entries/s over {len(ruleset.rules)} rules"
    )
    assert report.totals.entries >= 120_000
    assert elapsed < 60, f"classification took {elapsed:.1f}s"
    conn.close()
