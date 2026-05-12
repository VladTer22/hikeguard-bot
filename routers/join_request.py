"""Handles incoming chat_join_request events.

Flow per event:
1. Record arrival into VelocityTracker.
2. If chat is in raid mode → auto-decline, mark source 'raid_mode'.
3. Else compute score; map to auto-decline / auto-approve / grey-zone.
4. Auto decisions: call approve/decline Telegram API, persist outcome.
5. Grey-zone: persist as 'pending', notify admin chat with inline buttons
   handled by on_admin_approve / on_admin_decline below.
"""

import contextlib
import time
from typing import Any, Literal

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from config import Settings
from db.database import Database
from db.queries import JoinRequestQueries
from services.cas import CASChecker
from services.join_scorer import score_profile
from services.velocity_tracker import VelocityTracker
from utils import format_user, is_admin

logger = structlog.get_logger()
router = Router(name="join_request")

_tracker: VelocityTracker | None = None
_raid_announcements: dict[int, float] = {}

_SIGNAL_LABELS_UK = {
    "cas_hit": "у CAS-базі",
    "cjk_name": "ім'я ієрогліфами",
    "no_username": "без username",
    "has_username": "має username",
    "anglo_numeric_username": "username як у бота",
    "uid_very_fresh": "акаунт 2025+",
    "uid_fresh": "акаунт 2024+",
    "uid_old": "старий акаунт",
    "cyrillic_name": "кирилиця в імені",
    "is_premium": "Telegram Premium",
}


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
    if not signals:
        return "—"
    return ", ".join(
        f"{_SIGNAL_LABELS_UK.get(k, k)} ({v:+d})"
        for k, v in signals.items()
    )


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
) -> bool:
    user_display = format_user(user_id, username, full_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Дозволити", callback_data=f"jra:{chat_id}:{user_id}",
        ),
        InlineKeyboardButton(
            text="❌ Відхилити", callback_data=f"jrd:{chat_id}:{user_id}",
        ),
    ]])
    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🤔 <b>Підозріла заявка на вступ</b>\n"
                f"👤 {user_display}\n"
                f"Оцінка: <b>{score}</b> "
                f"(поріг відхилення ≥ {config.auto_decline_score})\n"
                f"Сигнали: <i>{_format_signals(signals)}</i>"
            ),
            reply_markup=kb,
        )
        return True
    except Exception:
        logger.warning("admin_queue_post_failed", user_id=user_id)
        return False


async def _announce_raid_start(
    bot: Bot, config: Settings, chat_id: int, now: float,
) -> None:
    last = _raid_announcements.get(chat_id, 0)
    if now - last < config.raid_announce_interval_sec:
        return
    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🚨 <b>Виявлено флуд join-заявок</b>\n"
                f"Чат: <code>{chat_id}</code>\n"
                f"Поріг: {config.raid_threshold}/{config.raid_window_sec}с\n"
                f"Авто-відхилення активне {config.raid_mode_minutes} хв."
            ),
        )
        _raid_announcements[chat_id] = now
    except Exception:
        logger.warning("raid_announce_failed", chat_id=chat_id)


async def _record_safe(
    queries: JoinRequestQueries,
    *,
    user_id: int,
    chat_id: int,
    username: str | None,
    full_name: str | None,
    score: int,
    signals: dict[str, int],
    decision: str,
    decision_source: str,
    decided_by: int | None = None,
) -> None:
    """DB write that never raises — logs and swallows errors so a DB hiccup
    after a successful Telegram action doesn't crash the handler."""
    try:
        await queries.record(
            user_id=user_id, chat_id=chat_id,
            username=username, full_name=full_name,
            score=score, signals=signals,
            decision=decision, decision_source=decision_source,
            decided_by=decided_by,
        )
    except Exception as e:
        logger.error(
            "join_request_record_failed",
            user_id=user_id, decision=decision, error=str(e),
        )


async def _resolve_safe(
    queries: JoinRequestQueries,
    *,
    user_id: int,
    chat_id: int,
    decision: str,
    decided_by: int,
) -> bool:
    try:
        return await queries.resolve_pending(
            user_id=user_id, chat_id=chat_id,
            decision=decision, decided_by=decided_by,
        )
    except Exception as e:
        logger.error(
            "join_request_resolve_failed",
            user_id=user_id, decision=decision, error=str(e),
        )
        return False


async def _decline_and_record(
    bot: Bot,
    queries: JoinRequestQueries,
    *,
    chat_id: int,
    user: Any,
    score: int,
    signals: dict[str, int],
    source: str,
    event_name: str,
) -> None:
    """Decline a join request, recording to DB regardless of API outcome."""
    try:
        await bot.decline_chat_join_request(chat_id, user.id)
        api_ok = True
    except TelegramRetryAfter as e:
        logger.warning("decline_flood_wait", user_id=user.id, retry_after=e.retry_after)
        api_ok = False
    except Exception as e:
        logger.warning("decline_failed", user_id=user.id, error=str(e))
        api_ok = False
    await _record_safe(
        queries,
        user_id=user.id, chat_id=chat_id,
        username=user.username, full_name=user.full_name,
        score=score, signals=signals,
        decision="decline", decision_source=source,
    )
    logger.info(
        event_name,
        user_id=user.id, chat_id=chat_id, score=score, api_ok=api_ok,
    )


async def _approve_and_record(
    bot: Bot,
    queries: JoinRequestQueries,
    *,
    chat_id: int,
    user: Any,
    score: int,
    signals: dict[str, int],
) -> None:
    """Approve a join request, recording to DB regardless of API outcome."""
    try:
        await bot.approve_chat_join_request(chat_id, user.id)
        api_ok = True
    except TelegramRetryAfter as e:
        logger.warning("approve_flood_wait", user_id=user.id, retry_after=e.retry_after)
        api_ok = False
    except Exception as e:
        logger.warning("approve_failed", user_id=user.id, error=str(e))
        api_ok = False
    await _record_safe(
        queries,
        user_id=user.id, chat_id=chat_id,
        username=user.username, full_name=user.full_name,
        score=score, signals=signals,
        decision="approve", decision_source="auto",
    )
    logger.info(
        "join_request_auto_approve",
        user_id=user.id, score=score, signals=signals, api_ok=api_ok,
    )


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

    if in_raid:
        await _announce_raid_start(bot, config, chat_id, now)
        await _decline_and_record(
            bot, queries, chat_id=chat_id, user=user,
            score=0, signals={}, source="raid_mode", event_name="join_request_raid_decline",
        )
        return

    # Outside raid: full scoring (includes external CAS lookup)
    cas_hit = await cas_checker.is_banned(user.id)
    result = score_profile(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        is_premium=bool(user.is_premium),
        cas_hit=cas_hit,
    )

    if result.score >= config.auto_decline_score:
        await _decline_and_record(
            bot, queries, chat_id=chat_id, user=user,
            score=result.score, signals=result.signals,
            source="auto", event_name="join_request_auto_decline",
        )
        return

    if result.score <= config.auto_approve_score:
        await _approve_and_record(
            bot, queries, chat_id=chat_id, user=user,
            score=result.score, signals=result.signals,
        )
        return

    # Grey zone — post admin card first; if posting fails, decline outright
    # (better to give the user a clear decline than leave them stuck forever)
    posted = await _admin_queue_post(
        bot, config,
        chat_id=chat_id, user_id=user.id,
        username=user.username, full_name=user.full_name,
        score=result.score, signals=result.signals,
    )
    if not posted:
        logger.error(
            "admin_queue_unreachable_declining",
            user_id=user.id, score=result.score,
        )
        await _decline_and_record(
            bot, queries, chat_id=chat_id, user=user,
            score=result.score, signals=result.signals,
            source="admin_chat_unavailable",
            event_name="join_request_grey_zone_declined",
        )
        return

    await _record_safe(
        queries,
        user_id=user.id, chat_id=chat_id,
        username=user.username, full_name=user.full_name,
        score=result.score, signals=result.signals,
        decision="pending", decision_source="auto",
    )
    logger.info(
        "join_request_grey_zone",
        user_id=user.id, score=result.score, signals=result.signals,
    )


async def _handle_admin_decision(
    callback: CallbackQuery,
    bot: Bot,
    db: Database,
    *,
    action: Literal["approve", "decline"],
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

    api = (
        bot.approve_chat_join_request if action == "approve"
        else bot.decline_chat_join_request
    )
    err_verb = "дозволити" if action == "approve" else "відхилити"
    try:
        await api(chat_id, user_id)
    except Exception as e:
        await callback.answer(f"Не вдалось {err_verb}: {e}"[:200])
        return

    updated = await _resolve_safe(
        JoinRequestQueries(db),
        user_id=user_id, chat_id=chat_id,
        decision=action, decided_by=callback.from_user.id,
    )

    done_verb = "Дозволено" if action == "approve" else "Відхилено"
    emoji = "✅" if action == "approve" else "❌"
    done_phrase = "Заявку дозволено" if action == "approve" else "Заявку відхилено"
    await callback.answer(
        done_verb if updated else f"{done_verb} (без запису в БД)"
    )
    admin_display = format_user(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name,
    )
    with contextlib.suppress(Exception):
        await callback.message.edit_text(
            (callback.message.html_text or "")
            + f"\n\n{emoji} <b>{done_phrase}</b> — {admin_display}",
            reply_markup=None,
        )
    logger.info(
        f"join_request_admin_{action}",
        user_id=user_id, by=callback.from_user.id,
    )


@router.callback_query(F.data.startswith("jra:"))
async def on_admin_approve(
    callback: CallbackQuery, bot: Bot, db: Database,
) -> None:
    await _handle_admin_decision(callback, bot, db, action="approve")


@router.callback_query(F.data.startswith("jrd:"))
async def on_admin_decline(
    callback: CallbackQuery, bot: Bot, db: Database,
) -> None:
    await _handle_admin_decision(callback, bot, db, action="decline")
