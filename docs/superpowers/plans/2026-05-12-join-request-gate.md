# Chat Join Request Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `chat_join_request` handler that surge-gates and signal-scores incoming join requests so the chat can stay publicly searchable without bots polluting the member list.

**Architecture:** A new aiogram router listens to `chat_join_request` events. Each event passes through (1) a per-chat sliding-window **velocity tracker** that flips into "raid mode" on bursts and auto-declines everything for a configurable cool-down, and (2) a pure **signal scorer** that combines profile features (`user_id` age proxy, username presence/pattern, name script, CAS hit) into a score that maps to auto-decline / auto-approve / admin-queue. Grey-zone requests post inline approve/decline buttons into the admin chat. All decisions persist to a new `join_requests` table for audit and future tuning.

**Tech Stack:** Python 3.12, aiogram 3, aiosqlite, structlog. New dev dependency: pytest + pytest-asyncio for pure-logic unit tests (project currently has no tests).

---

## File Structure

**New:**
- `services/velocity_tracker.py` — pure per-chat sliding-window state machine, no I/O
- `services/join_scorer.py` — pure profile → (score, signals) function, no I/O
- `routers/join_request.py` — aiogram handler + admin-queue callbacks
- `tests/__init__.py` — empty
- `tests/conftest.py` — pytest config
- `tests/test_velocity_tracker.py` — unit tests for the tracker
- `tests/test_join_scorer.py` — unit tests for the scorer

**Modified:**
- `config.py` — 5 new env-tunable settings
- `db/database.py` — add `join_requests` table to SCHEMA
- `db/queries.py` — add `JoinRequestQueries` class
- `bot.py` — wire new router, pass `cas_checker` into it
- `pyproject.toml` — add pytest, pytest-asyncio to dev deps
- `.env.example` — document new env vars
- `README.md` — describe new feature

**Splitting rationale:** scorer and tracker are pure functions — easy to unit test, easy to evolve. The handler glues them to aiogram. DB queries colocate with existing query classes. This mirrors the existing structure (`services/spam_detector.py`, `routers/admin.py`, `db/queries.py`).

---

## Background: Data Used for Threshold Calibration

Profile of the 11-May raid (2255 confirmed bots):

| Signal | % of bots |
|---|---|
| `user_id > 7_000_000_000` (acct created ≥2024) | 76.7% |
| `user_id > 8_000_000_000` (acct created ≥2025) | 51.7% |
| no `username` | 20.0% |
| CJK characters in `full_name` | 43.3% |
| `Anglo_Name<digits>` username pattern | 51.7% |
| Cyrillic in `full_name` | 23.6% |

Real Ukrainian user expected profile: has username, Cyrillic full name, user_id < 5B, not in CAS, sometimes Premium. The scoring weights in Task 3 are calibrated so that profile sums to negative (auto-approve) while every bot pattern above sums to ≥5 (auto-decline).

---

## Task 1: Config + DB schema + env example

**Files:**
- Modify: `config.py:1-35`
- Modify: `db/database.py:8-49`
- Modify: `db/queries.py` — append new class at end
- Modify: `.env.example`

- [ ] **Step 1.1: Add settings to `config.py`**

Replace the `Database` block in `config.py:26-28` and append new fields before line 28:

```python
    # Database
    db_path: str = "data/hikeguard.db"

    # Join request gate
    join_gate_enabled: bool = True
    raid_threshold: int = 10          # requests within window to trigger raid mode
    raid_window_sec: int = 60         # sliding window size
    raid_mode_minutes: int = 20       # how long raid mode stays on after last surge
    auto_decline_score: int = 5       # score >= this → auto-decline
    auto_approve_score: int = 0       # score <= this → auto-approve, else admin queue
```

- [ ] **Step 1.2: Add `join_requests` table to schema**

In `db/database.py:8-49`, append a new `CREATE TABLE` block inside the `SCHEMA` string (after the `gemini_cache` table, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS join_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    request_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score INTEGER,
    signals TEXT,
    decision TEXT,                       -- 'approve' | 'decline' | 'pending'
    decision_source TEXT,                -- 'auto' | 'admin' | 'raid_mode'
    decided_by INTEGER,
    decided_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_join_requests_date ON join_requests(request_date);
CREATE INDEX IF NOT EXISTS idx_join_requests_decision ON join_requests(decision);
```

- [ ] **Step 1.3: Add `JoinRequestQueries` to `db/queries.py`**

Append at the end of `db/queries.py`:

```python
import json
from datetime import datetime


class JoinRequestQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str | None,
        score: int,
        signals: dict,
        decision: str,
        decision_source: str,
        decided_by: int | None = None,
    ) -> None:
        decided_at = datetime.utcnow() if decision != "pending" else None
        await self._db.db.execute(
            """
            INSERT INTO join_requests
                (user_id, chat_id, username, full_name, score, signals,
                 decision, decision_source, decided_by, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, chat_id, username, full_name, score,
                json.dumps(signals, ensure_ascii=False),
                decision, decision_source, decided_by, decided_at,
            ),
        )
        await self._db.db.commit()

    async def resolve_pending(
        self,
        *,
        user_id: int,
        chat_id: int,
        decision: str,
        decided_by: int,
    ) -> bool:
        """Update the most recent pending entry for (user_id, chat_id).
        Returns True if a row was updated, False otherwise."""
        cursor = await self._db.db.execute(
            """
            UPDATE join_requests
            SET decision = ?, decision_source = 'admin',
                decided_by = ?, decided_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM join_requests
                WHERE user_id = ? AND chat_id = ? AND decision = 'pending'
                ORDER BY request_date DESC LIMIT 1
            )
            """,
            (decision, decided_by, user_id, chat_id),
        )
        await self._db.db.commit()
        return cursor.rowcount > 0

    async def get_stats(self, hours: int = 24) -> dict:
        cursor = await self._db.db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN decision = 'approve' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN decision = 'decline' THEN 1 ELSE 0 END) AS declined,
                SUM(CASE WHEN decision = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN decision_source = 'raid_mode' THEN 1 ELSE 0 END) AS raid_declined
            FROM join_requests
            WHERE request_date >= datetime('now', ?)
            """,
            (f"-{hours} hours",),
        )
        row = await cursor.fetchone()
        return {
            "total": row["total"] or 0,
            "approved": row["approved"] or 0,
            "declined": row["declined"] or 0,
            "pending": row["pending"] or 0,
            "raid_declined": row["raid_declined"] or 0,
        }
```

- [ ] **Step 1.4: Update `.env.example`**

Append to `.env.example`:

```
# Join request gate
JOIN_GATE_ENABLED=true
RAID_THRESHOLD=10
RAID_WINDOW_SEC=60
RAID_MODE_MINUTES=20
AUTO_DECLINE_SCORE=5
AUTO_APPROVE_SCORE=0
```

- [ ] **Step 1.5: Verify migration runs cleanly**

Run: `python -c "import asyncio; from db.database import Database; asyncio.run(Database('data/test_migration.db').init())"`
Then: `sqlite3 data/test_migration.db ".schema join_requests"` (if sqlite3 not available, use `python -c "import sqlite3; print(sqlite3.connect('data/test_migration.db').execute('SELECT sql FROM sqlite_master WHERE name=\"join_requests\"').fetchone())"`)
Expected: prints the CREATE TABLE statement.
Cleanup: `rm data/test_migration.db data/test_migration.db-*`

- [ ] **Step 1.6: Commit**

```bash
git add config.py db/database.py db/queries.py .env.example
git commit -m "feat: add join_requests schema and gate config"
```

---

## Task 2: Velocity Tracker (pure logic + tests)

**Files:**
- Create: `services/velocity_tracker.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_velocity_tracker.py`
- Modify: `pyproject.toml`

- [ ] **Step 2.1: Add pytest deps**

Edit `pyproject.toml`, replace the `[dependency-groups]` block:

```toml
[dependency-groups]
dev = [
    "ruff (>=0.15.4,<0.16.0)",
    "pytest (>=8.0.0)",
    "pytest-asyncio (>=0.24.0)"
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Then run: `poetry install` (or `.venv/bin/pip install pytest pytest-asyncio` if poetry not available locally).

- [ ] **Step 2.2: Write the failing test**

Create `tests/__init__.py` (empty file).

Create `tests/conftest.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

Create `tests/test_velocity_tracker.py`:

```python
from services.velocity_tracker import VelocityTracker


def test_below_threshold_does_not_trigger_raid() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(9):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is False


def test_threshold_reached_triggers_raid() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is True


def test_raid_mode_expires_after_duration() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is True
    # 20 minutes = 1200 seconds later
    assert tracker.in_raid_mode(chat_id=1, now=10 + 1201) is False


def test_old_events_outside_window_dont_count() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    # First check is at ts=10, raid mode triggers
    assert tracker.in_raid_mode(chat_id=1, now=10) is True
    # But if we never checked and only check 200s later with no new events,
    # the window has expired, raid mode was never set
    tracker2 = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker2.record(chat_id=1, ts=i)
    # At ts=200, window=60 → only events from ts>=140 count; we have none
    assert tracker2.in_raid_mode(chat_id=1, now=200) is False


def test_separate_chats_have_independent_state() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    assert tracker.in_raid_mode(chat_id=1, now=10) is True
    assert tracker.in_raid_mode(chat_id=2, now=10) is False


def test_raid_mode_extends_on_continued_burst() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    for i in range(10):
        tracker.record(chat_id=1, ts=i)
    tracker.in_raid_mode(chat_id=1, now=10)  # triggers raid_until = 10 + 1200 = 1210
    # 19 minutes in (still raid), another burst extends the timer
    for i in range(1140, 1150):
        tracker.record(chat_id=1, ts=i)
    tracker.in_raid_mode(chat_id=1, now=1150)  # extends to 1150 + 1200
    # At 1300 (well past original expiry of 1210), still in raid mode
    assert tracker.in_raid_mode(chat_id=1, now=1300) is True


def test_window_eviction_keeps_memory_bounded() -> None:
    tracker = VelocityTracker(threshold=10, window_sec=60, raid_minutes=20)
    # Hammer with 10000 old events, none should be retained at t=10000
    for i in range(10000):
        tracker.record(chat_id=1, ts=i)
    tracker.in_raid_mode(chat_id=1, now=10000)
    assert len(tracker._events[1]) <= 100
```

- [ ] **Step 2.3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_velocity_tracker.py -v`
Expected: 7 tests fail with `ImportError` (module doesn't exist yet).

- [ ] **Step 2.4: Implement `VelocityTracker`**

Create `services/velocity_tracker.py`:

```python
"""Per-chat sliding window of recent join requests.

Triggers 'raid mode' when burst exceeds threshold. Raid mode lasts
for raid_minutes after the last triggering event. Independent state
per chat_id. No I/O — caller injects timestamps for testability.
"""

from collections import defaultdict, deque


class VelocityTracker:
    def __init__(self, threshold: int, window_sec: int, raid_minutes: int) -> None:
        self.threshold = threshold
        self.window_sec = window_sec
        self.raid_duration_sec = raid_minutes * 60
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._raid_until: dict[int, float] = {}

    def record(self, *, chat_id: int, ts: float) -> None:
        """Append a join event at the given timestamp."""
        self._events[chat_id].append(ts)

    def in_raid_mode(self, *, chat_id: int, now: float) -> bool:
        """Return True if chat is currently in raid mode at `now`.

        Side effects: evicts events outside the window; if the in-window
        count crosses the threshold, (re)sets the raid_until timestamp
        to `now + raid_duration_sec`.
        """
        events = self._events[chat_id]
        cutoff = now - self.window_sec
        while events and events[0] < cutoff:
            events.popleft()

        if len(events) >= self.threshold:
            self._raid_until[chat_id] = now + self.raid_duration_sec

        raid_until = self._raid_until.get(chat_id, 0)
        return now < raid_until
```

- [ ] **Step 2.5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_velocity_tracker.py -v`
Expected: 7 passed.

- [ ] **Step 2.6: Commit**

```bash
git add services/velocity_tracker.py tests/__init__.py tests/conftest.py tests/test_velocity_tracker.py pyproject.toml
git commit -m "feat: add VelocityTracker for join-request burst detection"
```

---

## Task 3: Join Scorer (pure logic + tests)

**Files:**
- Create: `services/join_scorer.py`
- Create: `tests/test_join_scorer.py`

- [ ] **Step 3.1: Write the failing tests**

Create `tests/test_join_scorer.py`:

```python
from services.join_scorer import ScoreResult, score_profile


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
```

- [ ] **Step 3.2: Run tests to verify failure**

Run: `.venv/bin/pytest tests/test_join_scorer.py -v`
Expected: 8 tests fail with ImportError.

- [ ] **Step 3.3: Implement `score_profile`**

Create `services/join_scorer.py`:

```python
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
    return any(
        "぀" <= ch <= "鿿" or "゠" <= ch <= "ヿ"
        for ch in s
    )


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

    if _has_cjk(name):
        signals["cjk_name"] = 10

    if username is None:
        signals["no_username"] = 3
    else:
        signals["has_username"] = -1
        if _ANGLO_NUMERIC_RE.match(username):
            signals["anglo_numeric_username"] = 5

    if user_id > 8_000_000_000:
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
```

- [ ] **Step 3.4: Run tests to verify pass**

Run: `.venv/bin/pytest tests/test_join_scorer.py -v`
Expected: 8 passed.

- [ ] **Step 3.5: Backtest against the 2255 banned bots (sanity check)**

Run from project root:

```bash
.venv/bin/python -c "
import sqlite3, json
from services.join_scorer import score_profile

c = sqlite3.connect('data/hikeguard.db')
c.row_factory = sqlite3.Row
rows = c.execute('''
SELECT user_id, username, full_name, is_banned
FROM users
WHERE join_date >= \"2026-05-11 13:20:00\" AND join_date < \"2026-05-11 13:24:00\"
''').fetchall()

decline = approve = grey = 0
for r in rows:
    res = score_profile(
        user_id=r['user_id'], username=r['username'], full_name=r['full_name'],
        is_premium=False, cas_hit=False,
    )
    if res.score >= 5: decline += 1
    elif res.score <= 0: approve += 1
    else: grey += 1
print(f'Of {len(rows)} known bots: decline={decline} ({100*decline/len(rows):.1f}%), grey={grey} ({100*grey/len(rows):.1f}%), approve={approve} ({100*approve/len(rows):.1f}%)')
"
```

Expected: decline >= 60%, approve <= 10%. (If approve > 10%, surface the misclassified profiles by also printing samples of false-negatives and adjust weights in Task 3.3 before continuing.) The remaining ~40% will be caught by the velocity gate during the burst — backtest validates the scorer is genuinely picking up signal, not that it covers 100% alone.

**Note:** this backtest reads against the production-shaped DB. If running locally without `data/hikeguard.db` containing real data, copy from server first: `scp -i ~/.ssh/vladter22 root@46.225.232.142:/opt/hikeguard/data/hikeguard.db /tmp/prod-hg.db` and adjust the connect path.

- [ ] **Step 3.6: Commit**

```bash
git add services/join_scorer.py tests/test_join_scorer.py
git commit -m "feat: add JoinScorer for chat_join_request signal scoring"
```

---

## Task 4: Router skeleton — handler with auto-decisions only (no admin queue yet)

**Files:**
- Create: `routers/join_request.py`
- Modify: `bot.py:1-101`

- [ ] **Step 4.1: Implement the handler (auto-only, admin queue placeholder)**

Create `routers/join_request.py`:

```python
"""Handles incoming chat_join_request events.

Flow per event:
1. Record arrival into VelocityTracker.
2. If chat is in raid mode → auto-decline, mark source 'raid_mode'.
3. Else compute score; map to auto-decline / auto-approve / grey-zone.
4. Auto decisions: call approve/decline Telegram API, persist outcome.
5. Grey-zone: persist as 'pending', notify admin chat with inline buttons.
   (Admin-queue callbacks live in this same router — added in Task 5.)
"""

import time

import structlog
from aiogram import Bot, Router
from aiogram.types import ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup

from config import Settings
from db.database import Database
from db.queries import JoinRequestQueries
from services.cas import CASChecker
from services.join_scorer import score_profile
from services.velocity_tracker import VelocityTracker

logger = structlog.get_logger()
router = Router(name="join_request")

_tracker: VelocityTracker | None = None
_raid_announcements: dict[int, float] = {}  # chat_id → ts of last admin notification
_RAID_ANNOUNCE_INTERVAL = 600  # 10 min between consecutive raid-start pings per chat


def _get_tracker(config: Settings) -> VelocityTracker:
    global _tracker
    if _tracker is None:
        _tracker = VelocityTracker(
            threshold=config.raid_threshold,
            window_sec=config.raid_window_sec,
            raid_minutes=config.raid_mode_minutes,
        )
    return _tracker


def _format_signals(signals: dict[str, int]) -> str:
    return ", ".join(f"{k}({v:+d})" for k, v in signals.items()) or "—"


async def _admin_queue_post(
    bot: Bot,
    config: Settings,
    *,
    chat_id: int,
    user_id: int,
    username: str | None,
    full_name: str | None,
    score: int,
    signals: dict[str, int],
) -> None:
    display = f"@{username}" if username else full_name or f"ID:{user_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Approve", callback_data=f"jra:{chat_id}:{user_id}",
        ),
        InlineKeyboardButton(
            text="❌ Decline", callback_data=f"jrd:{chat_id}:{user_id}",
        ),
    ]])
    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🤔 <b>Підозріла заявка на вступ</b>\n"
                f"👤 {display} (ID: <code>{user_id}</code>)\n"
                f"Score: <b>{score}</b> "
                f"(decline≥{config.auto_decline_score}, approve≤{config.auto_approve_score})\n"
                f"Сигнали: <i>{_format_signals(signals)}</i>"
            ),
            reply_markup=kb,
        )
    except Exception:
        logger.warning("admin_queue_post_failed", user_id=user_id)


async def _announce_raid_start(
    bot: Bot, config: Settings, chat_id: int, now: float,
) -> None:
    last = _raid_announcements.get(chat_id, 0)
    if now - last < _RAID_ANNOUNCE_INTERVAL:
        return
    _raid_announcements[chat_id] = now
    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🚨 <b>Виявлено флуд join-заявок</b>\n"
                f"Чат: <code>{chat_id}</code>\n"
                f"Поріг: {config.raid_threshold}/{config.raid_window_sec}с\n"
                f"Авто-decline активний {config.raid_mode_minutes} хв."
            ),
        )
    except Exception:
        logger.warning("raid_announce_failed", chat_id=chat_id)


@router.chat_join_request()
async def on_join_request(
    event: ChatJoinRequest,
    bot: Bot,
    db: Database,
    config: Settings,
    cas_checker: CASChecker,
) -> None:
    if not config.join_gate_enabled:
        return

    user = event.from_user
    chat_id = event.chat.id
    now = time.monotonic()

    tracker = _get_tracker(config)
    tracker.record(chat_id=chat_id, ts=now)
    in_raid = tracker.in_raid_mode(chat_id=chat_id, now=now)

    queries = JoinRequestQueries(db)

    # CAS check is shared between raid mode and normal mode — log signal anyway
    cas_hit = await cas_checker.is_banned(user.id)
    result = score_profile(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        is_premium=bool(getattr(user, "is_premium", False)),
        cas_hit=cas_hit,
    )

    if in_raid:
        await _announce_raid_start(bot, config, chat_id, now)
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except Exception as e:
            logger.warning("raid_decline_failed", user_id=user.id, error=str(e))
        await queries.record(
            user_id=user.id, chat_id=chat_id,
            username=user.username, full_name=user.full_name,
            score=result.score, signals=result.signals,
            decision="decline", decision_source="raid_mode",
        )
        logger.info(
            "join_request_raid_decline",
            user_id=user.id, chat_id=chat_id, score=result.score,
        )
        return

    if result.score >= config.auto_decline_score:
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except Exception as e:
            logger.warning("auto_decline_failed", user_id=user.id, error=str(e))
        await queries.record(
            user_id=user.id, chat_id=chat_id,
            username=user.username, full_name=user.full_name,
            score=result.score, signals=result.signals,
            decision="decline", decision_source="auto",
        )
        logger.info(
            "join_request_auto_decline",
            user_id=user.id, score=result.score, signals=result.signals,
        )
        return

    if result.score <= config.auto_approve_score:
        try:
            await bot.approve_chat_join_request(chat_id, user.id)
        except Exception as e:
            logger.warning("auto_approve_failed", user_id=user.id, error=str(e))
        await queries.record(
            user_id=user.id, chat_id=chat_id,
            username=user.username, full_name=user.full_name,
            score=result.score, signals=result.signals,
            decision="approve", decision_source="auto",
        )
        logger.info(
            "join_request_auto_approve",
            user_id=user.id, score=result.score,
        )
        return

    # Grey zone
    await queries.record(
        user_id=user.id, chat_id=chat_id,
        username=user.username, full_name=user.full_name,
        score=result.score, signals=result.signals,
        decision="pending", decision_source="auto",
    )
    await _admin_queue_post(
        bot, config,
        chat_id=chat_id, user_id=user.id,
        username=user.username, full_name=user.full_name,
        score=result.score, signals=result.signals,
    )
    logger.info(
        "join_request_grey_zone",
        user_id=user.id, score=result.score,
    )
```

- [ ] **Step 4.2: Wire the router in `bot.py`**

In `bot.py:13` change:

```python
from routers import admin, media, new_member, purge, text
```

to:

```python
from routers import admin, join_request, media, new_member, purge, text
```

In `bot.py:80-84` (after `dp.include_router(admin.router)` and `dp.include_router(purge.router)`), insert:

```python
    dp.include_router(join_request.router)
```

- [ ] **Step 4.3: Smoke-test imports**

Run: `.venv/bin/python -c "from routers import join_request; print('router:', join_request.router.name)"`
Expected: `router: join_request`

- [ ] **Step 4.4: Commit**

```bash
git add routers/join_request.py bot.py
git commit -m "feat: handle chat_join_request with raid gate and signal scoring"
```

---

## Task 5: Admin queue callbacks (approve / decline buttons)

**Files:**
- Modify: `routers/join_request.py` — append callback handlers

- [ ] **Step 5.1: Add callback handlers**

Append to `routers/join_request.py` (after the `on_join_request` function):

```python
from aiogram import F
from aiogram.types import CallbackQuery

from utils import is_admin


@router.callback_query(F.data.startswith("jra:"))
async def on_admin_approve(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
) -> None:
    if not callback.data or not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    chat_id, user_id = int(parts[1]), int(parts[2])

    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("Тільки адмін чату може вирішувати")
        return

    try:
        await bot.approve_chat_join_request(chat_id, user_id)
    except Exception as e:
        await callback.answer(f"Не вдалось approve: {e}")
        return

    updated = await JoinRequestQueries(db).resolve_pending(
        user_id=user_id, chat_id=chat_id,
        decision="approve", decided_by=callback.from_user.id,
    )
    await callback.answer("Approved" if updated else "Approved (без запису)")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n✅ <b>Approved</b> "
            f"by {callback.from_user.id}"
        )
    except Exception:
        pass
    logger.info(
        "join_request_admin_approve",
        user_id=user_id, by=callback.from_user.id,
    )


@router.callback_query(F.data.startswith("jrd:"))
async def on_admin_decline(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
) -> None:
    if not callback.data or not callback.from_user or not callback.message:
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    chat_id, user_id = int(parts[1]), int(parts[2])

    if not await is_admin(bot, chat_id, callback.from_user.id):
        await callback.answer("Тільки адмін чату може вирішувати")
        return

    try:
        await bot.decline_chat_join_request(chat_id, user_id)
    except Exception as e:
        await callback.answer(f"Не вдалось decline: {e}")
        return

    updated = await JoinRequestQueries(db).resolve_pending(
        user_id=user_id, chat_id=chat_id,
        decision="decline", decided_by=callback.from_user.id,
    )
    await callback.answer("Declined" if updated else "Declined (без запису)")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n❌ <b>Declined</b> "
            f"by {callback.from_user.id}"
        )
    except Exception:
        pass
    logger.info(
        "join_request_admin_decline",
        user_id=user_id, by=callback.from_user.id,
    )
```

Note: move all `from aiogram import ...` and `from aiogram.types import ...` to the top of the file, merging with the existing imports — Python `from aiogram import F` cannot sit mid-file in the final form. The block above shows them inline only for context; in the actual edit, consolidate at module top.

- [ ] **Step 5.2: Smoke-test imports again**

Run: `.venv/bin/python -c "from routers import join_request; print(len(join_request.router.callback_query.handlers))"`
Expected: `2` (the two new callback handlers).

- [ ] **Step 5.3: Commit**

```bash
git add routers/join_request.py
git commit -m "feat: add admin approve/decline callbacks for grey-zone join requests"
```

---

## Task 6: Extend `/status` with join-request stats

**Files:**
- Modify: `routers/admin.py:797-826` (the `cmd_status` function)

- [ ] **Step 6.1: Read existing `cmd_status`**

Read `routers/admin.py:797-826` to confirm the current shape (already known from drafting; verify nothing changed since).

- [ ] **Step 6.2: Add join-request stats to `/status` output**

In `routers/admin.py`, at top of file add to imports:

```python
from db.queries import JoinRequestQueries
```

Then in `cmd_status`, modify the function. Replace the body from `stats_24h = ...` through `reply = await message.reply(text)` with:

```python
    spam_log = SpamLogQueries(db)
    join_q = JoinRequestQueries(db)

    stats_24h = await spam_log.get_stats(hours=24)
    stats_7d = await spam_log.get_stats(hours=168)
    stats_all = await spam_log.get_stats()
    top_methods = await spam_log.get_top_methods()
    join_24h = await join_q.get_stats(hours=24)
    join_7d = await join_q.get_stats(hours=168)

    methods_text = "\n".join(
        f"  • {method}: {count}" for method, count in top_methods
    ) or "  Ще немає даних"

    text = (
        f"📊 <b>Статистика HikeGuard</b>\n\n"
        f"<b>Спам видалено:</b>\n"
        f"  • 24 год: {stats_24h['total']}\n"
        f"  • 7 днів: {stats_7d['total']}\n"
        f"  • Всього: {stats_all['total']}\n\n"
        f"<b>Бани:</b>\n"
        f"  • 24 год: {stats_24h['bans']}\n"
        f"  • 7 днів: {stats_7d['bans']}\n\n"
        f"<b>Join-заявки (24 год / 7 днів):</b>\n"
        f"  • Approve: {join_24h['approved']} / {join_7d['approved']}\n"
        f"  • Decline: {join_24h['declined']} / {join_7d['declined']}\n"
        f"  • Raid-decline: {join_24h['raid_declined']} / {join_7d['raid_declined']}\n"
        f"  • Pending: {join_24h['pending']} / {join_7d['pending']}\n\n"
        f"<b>Методи виявлення:</b>\n{methods_text}"
    )

    reply = await message.reply(text)
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=60)
```

- [ ] **Step 6.3: Smoke-test imports**

Run: `.venv/bin/python -c "from routers import admin; print('ok')"`
Expected: `ok`.

- [ ] **Step 6.4: Commit**

```bash
git add routers/admin.py
git commit -m "feat: include join-request stats in /status output"
```

---

## Task 7: Documentation, deploy notes, final wire-up

**Files:**
- Modify: `README.md`
- Verify all earlier changes import together

- [ ] **Step 7.1: Update README**

In `README.md`, add a new section **after** the "Detection layers" section and **before** "Moderation actions":

```markdown
## Join-request gate

When the chat is configured with "approve to join" (set in Telegram chat settings → permissions), the bot intercepts every incoming join request and decides automatically:

- **Raid mode** — if more than `RAID_THRESHOLD` requests arrive within `RAID_WINDOW_SEC` seconds, the bot enters raid mode for `RAID_MODE_MINUTES` minutes and declines every request during that window. Designed for the realistic attack shape: bursts of hundreds to thousands of requests in seconds.
- **Signal scoring** outside raid mode — combines `user_id` age proxy (≥7B/8B = fresh account), username presence and pattern (anglo+digits style is a known botnet signature), full-name script (CJK in a Ukrainian chat is a strong signal), Telegram Premium status, and CAS hit. Score ≥ `AUTO_DECLINE_SCORE` auto-declines; score ≤ `AUTO_APPROVE_SCORE` auto-approves; in-between is posted to the admin chat with inline approve/decline buttons.
- **Audit trail** — every decision is logged to the `join_requests` table; visible in aggregate via `/status`.

Bot needs admin permission to invite users (`can_invite_users`) for approve/decline to work.
```

Update the configuration table at the bottom of README to include:

```markdown
| `JOIN_GATE_ENABLED` | `true` | Master switch for the join-request gate |
| `RAID_THRESHOLD` | `10` | Requests in `RAID_WINDOW_SEC` to trigger raid mode |
| `RAID_WINDOW_SEC` | `60` | Sliding window size for raid detection |
| `RAID_MODE_MINUTES` | `20` | Duration of auto-decline mode after raid trigger |
| `AUTO_DECLINE_SCORE` | `5` | Score threshold for auto-decline |
| `AUTO_APPROVE_SCORE` | `0` | Score threshold for auto-approve (else admin queue) |
```

- [ ] **Step 7.2: Run full test suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: 15 passed (7 velocity + 8 scorer).

- [ ] **Step 7.3: Lint clean**

Run: `.venv/bin/ruff check services/join_scorer.py services/velocity_tracker.py routers/join_request.py db/queries.py config.py bot.py`
Expected: no errors.

- [ ] **Step 7.4: Smoke import the whole bot**

Run: `.venv/bin/python -c "import bot; print('bot.py imports cleanly')"`
Expected: `bot.py imports cleanly` (without starting the bot — `if __name__ == '__main__'` guard prevents polling).

- [ ] **Step 7.5: Commit**

```bash
git add README.md
git commit -m "docs: document join-request gate and new env vars"
```

- [ ] **Step 7.6: Verify migration on prod DB (dry-run)**

Run: `scp -i ~/.ssh/vladter22 root@46.225.232.142:/opt/hikeguard/data/hikeguard.db /tmp/prod-hg-preview.db`
Then: `.venv/bin/python -c "import asyncio; from db.database import Database; asyncio.run(Database('/tmp/prod-hg-preview.db').init())"`
Then: `.venv/bin/python -c "import sqlite3; print(sqlite3.connect('/tmp/prod-hg-preview.db').execute('SELECT sql FROM sqlite_master WHERE name=\"join_requests\"').fetchone())"`
Expected: prints the `CREATE TABLE` statement, no errors from existing data.
Cleanup: `rm /tmp/prod-hg-preview.db /tmp/prod-hg-preview.db-*`

- [ ] **Step 7.7: Deploy steps (manual, performed by operator)**

Document but do not execute as part of plan:

```bash
git push origin main
ssh -i ~/.ssh/vladter22 root@46.225.232.142 \
  "cd /opt/hikeguard && git pull && docker compose up -d --build --force-recreate"
```

After deploy, the chat owner must:
1. Promote @hikeguardbot to admin with **"Invite users via link"** permission (`can_invite_users=True`) — required for approve/decline join request API calls.
2. In chat settings → "Group type" → switch to public; enable "Approve new members".
3. Watch the admin chat for the raid announcement banner if a burst hits, and grey-zone notifications for individual reviews.

---

## Self-Review

**Spec coverage:**
- ✅ Velocity gate (Task 2 + Task 4)
- ✅ Multi-signal scoring (Task 3 + Task 4)
- ✅ Admin queue for grey zone (Task 4 + Task 5)
- ✅ Audit trail in DB (Task 1 + every persist call in Task 4/5)
- ✅ Visible stats (Task 6)
- ✅ Per-chat state (VelocityTracker is keyed by chat_id; tested in Task 2.2)
- ✅ Raid-mode announcement to admin (Task 4.1, throttled by `_RAID_ANNOUNCE_INTERVAL`)
- ✅ Config knobs (Task 1 + Task 7)
- ✅ Backtest on real data (Task 3.5)
- ✅ Documentation (Task 7)

**Placeholder scan:** every code step contains complete code; tests have full assertions; deploy commands are exact.

**Type consistency:**
- `ScoreResult(score, signals)` defined in Task 3.3, used identically in Task 4.1.
- `JoinRequestQueries.record(...)` keyword args in Task 1.3 match call sites in Task 4.1.
- `VelocityTracker(threshold, window_sec, raid_minutes)` constructor in Task 2.4 matches `_get_tracker` in Task 4.1.
- Callback prefixes `jra:` / `jrd:` consistent between handler post (Task 4.1) and callbacks (Task 5.1).

**Open trade-offs documented:**
- No integration tests for aiogram handlers — out-of-scope; the project has no test infrastructure and adding aiogram mocking would be scope creep. Pure-logic tests cover the hard parts.
- In-memory velocity state is lost on bot restart — acceptable; the worst case is a brief window where the first ~10 events of a restart-coincident raid escape into scoring (which still declines them by score).
