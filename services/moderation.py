"""Shared spam moderation logic: punish, log, notify."""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from html import escape

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from config import Settings
from db.database import Database
from db.queries import SpamLogQueries, UserQueries
from services.spam_detector import DetectionResult
from spam_replies import SPAM_REPLIES
from utils import auto_delete_message, format_user

logger = structlog.get_logger()


async def handle_spam(
    message: Message,
    bot: Bot,
    db: Database,
    config: Settings,
    result: DetectionResult,
) -> None:
    """Delete spam, punish user (mute/ban), log to DB, notify admin chat."""
    user: User = message.from_user  # type: ignore[assignment]
    chat_id = message.chat.id

    # Delete the spam message
    try:
        deleted = await message.delete()
        logger.info(
            "message_delete_result",
            message_id=message.message_id,
            chat_id=chat_id,
            result=deleted,
        )
    except Exception as e:
        logger.warning(
            "message_delete_failed",
            message_id=message.message_id,
            chat_id=chat_id,
            error=str(e),
            error_type=type(e).__name__,
        )

    # Notify the chat (auto-delete after 30 sec)
    notice = await bot.send_message(chat_id=chat_id, text=random.choice(SPAM_REPLIES))
    asyncio.create_task(auto_delete_message(bot, chat_id, notice.message_id, 30))

    # Ensure user exists in DB, then increment strikes
    users = UserQueries(db)
    await users.upsert_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
    )
    strikes = await users.increment_strikes(user.id)
    ban_threshold = await users.get_ban_threshold(user.id, config.ban_on_strike)

    # Punish: ban on threshold, mute otherwise
    action, action_text = await _apply_punishment(
        bot, users, config, chat_id, user.id, strikes, ban_threshold,
    )

    # Log to DB
    await SpamLogQueries(db).log_spam(
        user_id=user.id,
        chat_id=chat_id,
        message_id=message.message_id,
        detection_method=result.method,
        caption_text=result.caption_text[:500] if result.caption_text else None,
        spam_score=result.score,
        gemini_reason=result.gemini_result.reason if result.gemini_result else None,
        action_taken=action,
    )

    # Notify admin chat
    await _notify_admin_spam(bot, config, chat_id, user, result, action, action_text)

    logger.info(
        "spam_action_taken",
        user_id=user.id,
        action=action,
        method=result.method,
        score=result.score,
    )


async def notify_admins_uncertain(
    message: Message,
    bot: Bot,
    config: Settings,
    result: DetectionResult,
) -> None:
    """Forward uncertain message to admin chat for manual review."""
    user: User = message.from_user  # type: ignore[assignment]
    gemini_reason = result.gemini_result.reason if result.gemini_result else "?"
    confidence = result.gemini_result.confidence if result.gemini_result else 0.0

    try:
        await message.forward(chat_id=config.admin_chat_id)
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"⚠️ <b>Підозріле повідомлення (потрібна перевірка)</b>\n"
                f"👤 {format_user(user.id, user.username, user.full_name)}\n"
                f"🤖 Gemini: {escape(gemini_reason)} (confidence: {confidence:.0%})\n"
                f"📊 Keyword score: {result.score}"
            ),
        )
    except Exception:
        logger.warning("admin_uncertain_notify_failed")


async def _apply_punishment(
    bot: Bot,
    users: UserQueries,
    config: Settings,
    chat_id: int,
    user_id: int,
    strikes: int,
    ban_threshold: int,
) -> tuple[str, str]:
    """Apply mute or ban. Returns (action, action_text)."""
    try:
        if strikes >= ban_threshold:
            await bot.ban_chat_member(
                chat_id=chat_id, user_id=user_id, revoke_messages=True,
            )
            await users.set_banned(user_id)
            return "banned", f"бан (strike {strikes}/{ban_threshold})"

        base = config.mute_duration_minutes
        mute_minutes = base * (24 ** (strikes - 1))  # 60 → 1440 → ...
        until = datetime.now(tz=UTC) + timedelta(minutes=mute_minutes)
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        if mute_minutes >= 1440:
            duration_text = f"{mute_minutes // 1440} д"
        else:
            duration_text = f"{mute_minutes} хв"
        return (
            "muted",
            f"мут {duration_text} (strike {strikes}/{ban_threshold})",
        )
    except TelegramBadRequest as e:
        logger.warning("punish_failed", user_id=user_id, error=str(e))
        return "deleted", "тільки видалення (не вдалось обмежити)"


def mute_action_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard with Ban and Unmute buttons for admin notifications."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🚫 Забанити",
            callback_data=f"ab:{chat_id}:{user_id}",
        ),
        InlineKeyboardButton(
            text="🔊 Розмутити",
            callback_data=f"au:{chat_id}:{user_id}",
        ),
    ]])


async def _notify_admin_spam(
    bot: Bot,
    config: Settings,
    chat_id: int,
    user: User,
    result: DetectionResult,
    action: str,
    action_text: str,
) -> None:
    """Send spam report to admin chat."""
    keywords_str = ", ".join(f"{k}({s})" for k, s in result.matched_keywords)
    gemini_line = ""
    if result.gemini_result:
        gemini_line = (
            f"\n🤖 Gemini: {escape(result.gemini_result.reason)} "
            f"(confidence: {result.gemini_result.confidence:.0%})"
        )

    caption_display = escape(result.caption_text[:200]) if result.caption_text else "—"
    keyboard = mute_action_keyboard(chat_id, user.id) if action == "muted" else None

    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🚫 <b>Спам видалено</b>\n"
                f"👤 {format_user(user.id, user.username, user.full_name)}\n"
                f"📊 Score: {result.score} | Метод: {result.method}\n"
                f"📝 Текст: <i>{caption_display}</i>\n"
                f"🔑 Keywords: {keywords_str or '—'}"
                f"{gemini_line}\n"
                f"⚡ Дія: {action_text}"
            ),
            reply_markup=keyboard,
        )
    except Exception:
        logger.warning("admin_notify_failed")
