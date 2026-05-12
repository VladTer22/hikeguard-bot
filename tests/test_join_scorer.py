from services.join_scorer import score_profile


def test_typical_ukrainian_user_auto_approves() -> None:
    result = score_profile(
        user_id=100_000_000,         # old account
        username="petro_k",
        full_name="Петро Шевченко",
        is_premium=False,
        cas_hit=False,
    )
    assert result.score <= 0
    assert "has_username" in result.signals
    assert "cyrillic_name" in result.signals


def test_cjk_name_is_strong_decline_signal() -> None:
    result = score_profile(
        user_id=8_200_000_000,
        username=None,
        full_name="奈飞合租 YouTube Disney",
        is_premium=False,
        cas_hit=False,
    )
    assert result.score >= 5
    assert "cjk_name" in result.signals


def test_anglo_numeric_username_pattern_is_decline_signal() -> None:
    result = score_profile(
        user_id=7_500_000_000,
        username="Jenny_Santiago50",
        full_name="平🐰",
        is_premium=False,
        cas_hit=False,
    )
    assert result.score >= 5
    assert "anglo_numeric_username" in result.signals


def test_no_username_plus_fresh_uid_is_decline() -> None:
    result = score_profile(
        user_id=7_500_000_000,
        username=None,
        full_name="John Smith",
        is_premium=False,
        cas_hit=False,
    )
    assert result.score >= 5
    assert "no_username" in result.signals
    assert "uid_fresh" in result.signals


def test_cas_hit_overrides_everything() -> None:
    result = score_profile(
        user_id=100_000_000,
        username="legit_user",
        full_name="Петро Шевченко",
        is_premium=True,
        cas_hit=True,
    )
    assert result.score >= 100
    assert "cas_hit" in result.signals


def test_premium_user_gets_bonus() -> None:
    result_no_premium = score_profile(
        user_id=8_000_000_000, username="x", full_name="Test",
        is_premium=False, cas_hit=False,
    )
    result_premium = score_profile(
        user_id=8_000_000_000, username="x", full_name="Test",
        is_premium=True, cas_hit=False,
    )
    assert result_premium.score < result_no_premium.score


def test_borderline_latin_no_signals_lands_in_grey_zone() -> None:
    # Plausible English-speaking traveler: latin name, has username,
    # moderately fresh account, no other red flags
    result = score_profile(
        user_id=7_200_000_000,
        username="alex_traveller",
        full_name="Alex Brown",
        is_premium=False,
        cas_hit=False,
    )
    assert 0 < result.score < 5


def test_signals_dict_includes_all_evaluated_rules() -> None:
    result = score_profile(
        user_id=8_000_000_000,
        username=None,
        full_name="奈飞",
        is_premium=False,
        cas_hit=False,
    )
    # signals dict captures both positive and negative findings
    for key in ("cjk_name", "no_username", "uid_very_fresh"):
        assert key in result.signals
    assert all(isinstance(v, int) for v in result.signals.values())
