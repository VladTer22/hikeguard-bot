"""Pure scoring of a Telegram user profile for join-request gating.

Higher score = more bot-like. Caller decides thresholds (auto_decline_score,
auto_approve_score). signals dict captures the contribution of each rule
for audit/debugging.

Calibration source: profile of 2255 confirmed bots from raid on 2026-05-11
(see docs/superpowers/plans/2026-05-12-join-request-gate.md).
"""

import re
from dataclasses import dataclass

_ANGLO_NUMERIC_RE = re.compile(r"^[A-Z][a-zA-Z]+[_-]?[A-Za-z]*\d+$")


def _has_cjk(s: str) -> bool:
    # U+3040..U+9FFF covers Hiragana, Katakana, and CJK Unified Ideographs.
    return any("぀" <= ch <= "鿿" for ch in s)


def _has_cyrillic(s: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in s)


@dataclass
class ScoreResult:
    score: int
    signals: dict[str, int]


def score_profile(
    *,
    user_id: int,
    username: str | None,
    full_name: str | None,
    is_premium: bool,
    cas_hit: bool,
) -> ScoreResult:
    """Return aggregated score plus per-rule contribution."""
    name = full_name or ""
    signals: dict[str, int] = {}

    if cas_hit:
        signals["cas_hit"] = 100
        return ScoreResult(score=100, signals=signals)

    if _has_cjk(name):
        signals["cjk_name"] = 10

    if username is None:
        signals["no_username"] = 3
    else:
        signals["has_username"] = -1
        if _ANGLO_NUMERIC_RE.match(username):
            signals["anglo_numeric_username"] = 5

    # uid age proxy: >=8B very fresh, >7B fresh, <5B old.
    # Neutral zone [5B, 7B] gets no signal.
    if user_id >= 8_000_000_000:
        signals["uid_very_fresh"] = 3
    elif user_id > 7_000_000_000:
        signals["uid_fresh"] = 2
    elif user_id < 5_000_000_000:
        signals["uid_old"] = -2

    if _has_cyrillic(name):
        signals["cyrillic_name"] = -2

    if is_premium:
        signals["is_premium"] = -3

    return ScoreResult(score=sum(signals.values()), signals=signals)
