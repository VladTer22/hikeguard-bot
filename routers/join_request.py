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
from html import escape

import structlog
from aiogram import Bot, F, Router
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
from utils import is_admin

logger = structlog.get_logger()
router = Router(name="join_request")

_tracker: VelocityTracker | None = None
_raid_announcements: dict[int, float] = {}
_RAID_ANNOUNCE_INTERVAL = 600


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
    if username:
        display = f"@{escape(username)}"
    elif full_name:
        display = escape(full_name)
    else:
        display = f"ID:{user_id}"
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

    cas_hit = await cas_checker.is_banned(user.id)
    result = score_profile(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        is_premium=bool(user.is_premium),
        cas_hit=cas_hit,
    )

    if in_raid:
        await _announce_raid_start(bot, config, chat_id, now)
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except Exception as e:
            logger.warning("raid_decline_failed", user_id=user.id, error=str(e))
            return
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
            return
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
            return
        await queries.record(
            user_id=user.id, chat_id=chat_id,
            username=user.username, full_name=user.full_name,
            score=result.score, signals=result.signals,
            decision="approve", decision_source="auto",
        )
        logger.info(
            "join_request_auto_approve",
            user_id=user.id, score=result.score, signals=result.signals,
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
        user_id=user.id, score=result.score, signals=result.signals,
    )


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
