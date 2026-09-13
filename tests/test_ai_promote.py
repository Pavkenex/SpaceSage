"""Rule promotion: an accepted verdict one click away from a rule.

The AI never edits the rule pack by itself.  It proposes; the user accepts; the
promoted rule is a plain, path-anchored rule in the normal user pack, and the
engine's own loader is what decides it is valid.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ai_stub import classification_answer, item, suggestion_answer
from ai_support import fact
from spacesage import rules
from spacesage.ai import AIEngine
from spacesage.ai import promote as promote_engine
from spacesage.ai.promote import PromotionError
from spacesage.ai.prompts import parse_classifications, parse_suggestions

SETUP = "C:\\Users\\matija\\Downloads\\setup-old.msi"


def suggestion(**overrides: object) -> object:
    payload = suggestion_answer(item(SETUP))["suggestions"][0]
    payload.update(overrides)
    return parse_suggestions({"suggestions": [payload]})[0]


def classification(**overrides: object) -> object:
    payload = classification_answer(
        {
            "path": SETUP,
            "category": "installer",
            "tier": "T1",
            "action": "DELETE_QUARANTINE",
            "confidence": 0.9,
            "rationale": "an old installer that can be re-downloaded",
        }
    )["classifications"][0]
    payload.update(overrides)
    return parse_classifications({"classifications": [payload]})[0]


def test_a_classification_becomes_a_path_anchored_rule() -> None:
    parsed = classification()

    draft = promote_engine.draft_from_classification(fact(SETUP), parsed)

    assert draft.action == "DELETE_QUARANTINE"
    assert draft.tier == "T1"
    assert draft.category == "installer"
    assert draft.paths == (SETUP,) or draft.paths == (promote_engine.pattern_for(fact(SETUP)),)
    assert draft.id.startswith("promote-")
    assert draft.id == promote_engine.rule_id_for(SETUP)  # stable
    assert "AI" in draft.note


def test_a_suggestion_needs_a_tier_for_destructive_actions() -> None:
    plain = fact(SETUP)  # no tier: the rules never decided this row

    with pytest.raises(PromotionError) as caught:
        promote_engine.draft_from_suggestion(plain, suggestion(action="DELETE_QUARANTINE"))

    assert "T1/T2" in caught.value.message or "tier" in caught.value.message

    draft = promote_engine.draft_from_suggestion(
        plain, suggestion(action="DELETE_QUARANTINE"), tier="T2"
    )
    assert draft.tier == "T2"
    assert draft.action == "DELETE_QUARANTINE"


def test_a_low_confidence_destructive_action_is_refused() -> None:
    confident = fact(SETUP, tier="T1")

    with pytest.raises(PromotionError) as caught:
        promote_engine.draft_from_suggestion(
            confident, suggestion(action="DELETE_QUARANTINE", confidence=0.55)
        )

    assert "0.55" in caught.value.message
    assert "REVIEW" in caught.value.hint


def test_a_safe_action_is_promoted_at_any_confidence() -> None:
    draft = promote_engine.draft_from_suggestion(
        fact(SETUP), suggestion(action="REVIEW", confidence=0.2)
    )

    assert draft.action == "REVIEW"


def test_the_draft_knows_its_pattern() -> None:
    assert promote_engine.pattern_for(fact(SETUP)) == SETUP
    assert promote_engine.pattern_for(fact("C:\\Temp", is_dir=True)) == "C:\\Temp/**"
    assert promote_engine.pattern_for(fact("C:\\Temp\\", is_dir=True)) == "C:\\Temp/**"
    assert promote_engine.rule_id_for(SETUP).startswith("promote-")
    assert promote_engine.rule_id_for(SETUP) == promote_engine.rule_id_for(SETUP)


def test_writing_produces_a_pack_the_engine_can_load(tmp_path: Path) -> None:
    draft = promote_engine.draft_from_classification(fact(SETUP), classification())

    result = promote_engine.write_rules([draft], dest_dir=tmp_path)

    assert result.ok is True
    assert result.path == tmp_path / "ai-promoted.toml"
    assert result.written == (draft.id,)
    assert result.fingerprint
    assert result.toml
    loaded = rules.load_rules(user_dir=tmp_path)
    assert any(rule.id == draft.id for rule in loaded.rules)
    promoted = next(rule for rule in loaded.rules if rule.id == draft.id)
    assert promoted.action == "DELETE_QUARANTINE"


def test_every_rule_in_the_pack_round_trips(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        draft = promote_engine.draft_from_classification(
            fact(f"C:\\Temp\\{name}.msi"), classification(path=f"C:\\Temp\\{name}.msi")
        )
        promote_engine.write_rules([draft], dest_dir=tmp_path)

    loaded = rules.load_rules(user_dir=tmp_path)
    promoted = [rule for rule in loaded.rules if rule.id.startswith("promote-")]

    assert len(promoted) == 3
    assert {rule.action for rule in promoted} == {"DELETE_QUARANTINE"}


def test_a_second_promotion_merges_and_a_repeat_replaces(tmp_path: Path) -> None:
    first = promote_engine.draft_from_classification(
        fact("C:\\Temp\\a.msi"), classification(path="C:\\Temp\\a.msi")
    )
    second = promote_engine.draft_from_classification(
        fact("C:\\Temp\\b.msi"), classification(path="C:\\Temp\\b.msi")
    )
    promote_engine.write_rules([first], dest_dir=tmp_path)

    merged = promote_engine.write_rules([second], dest_dir=tmp_path)
    again = replace(first, confidence=0.99)
    repeated = promote_engine.write_rules([again], dest_dir=tmp_path)

    assert merged.written == (second.id,)
    assert repeated.written == ()
    assert repeated.replaced == (first.id,)
    loaded = rules.load_rules(user_dir=tmp_path)
    promoted = {rule.id for rule in loaded.rules if rule.id.startswith("promote-")}
    assert promoted == {first.id, second.id}
    updated = next(rule for rule in loaded.rules if rule.id == first.id)
    assert updated.confidence == 0.99


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    draft = promote_engine.draft_from_classification(fact(SETUP), classification())

    result = promote_engine.write_rules([draft], dest_dir=tmp_path, dry_run=True)

    assert result.ok is True
    assert result.dry_run is True
    assert not (tmp_path / "ai-promoted.toml").exists()
    assert "DELETE_QUARANTINE" in result.toml


def test_an_invalid_draft_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    good = promote_engine.draft_from_classification(fact(SETUP), classification())
    broken = replace(good, action="SHRED_EVERYTHING")

    result = promote_engine.write_rules([good, broken], dest_dir=tmp_path)

    assert result.ok is False
    assert result.error is not None
    assert not (tmp_path / "ai-promoted.toml").exists()


def test_an_existing_broken_pack_is_never_overwritten(tmp_path: Path) -> None:
    pack = tmp_path / "ai-promoted.toml"
    pack.write_text("[[rule]]\nid = 'x'\n", encoding="utf-8")  # missing everything
    draft = promote_engine.draft_from_classification(fact(SETUP), classification())

    result = promote_engine.write_rules([draft], dest_dir=tmp_path)

    assert result.ok is False
    assert result.error is not None
    assert pack.read_text(encoding="utf-8") == "[[rule]]\nid = 'x'\n"


def test_the_engine_promotes_a_suggestion_without_raising(tmp_path: Path) -> None:
    engine = AIEngine()
    accepted = suggestion(action="DELETE_QUARANTINE", confidence=0.9)

    refused = engine.promote_suggestion(fact(SETUP), accepted, dest_dir=tmp_path)

    assert refused.ok is False  # no tier yet: refused, not an exception
    assert refused.error is not None
    assert not tmp_path.exists() or not (tmp_path / "ai-promoted.toml").exists()

    allowed = engine.promote_suggestion(fact(SETUP, tier="T1"), accepted, dest_dir=tmp_path)
    assert allowed.ok is True
    assert (tmp_path / "ai-promoted.toml").is_file()


def test_the_engine_promotes_a_classification(tmp_path: Path) -> None:
    engine = AIEngine()

    result = engine.promote_classification(fact(SETUP), classification(), dest_dir=tmp_path)

    assert result.ok is True
    assert result.written
    loaded = rules.load_rules(user_dir=tmp_path)
    assert any("setup-old" in rule.rationale or rule.id for rule in loaded.rules)


def test_every_promotion_error_is_an_ai_error() -> None:
    # the UI switches on AIError and shows `hint`; promotion must not invent a
    # second, unrelated exception type
    from spacesage.ai import AIError, PromotionError

    assert issubclass(PromotionError, AIError)
    broken = replace(
        promote_engine.draft_from_classification(fact(SETUP), classification()), action=""
    )
    result = promote_engine.write_rules([broken], dest_dir="/proc/definitely/not/writable")
    assert result.ok is False and isinstance(result.error, AIError)
