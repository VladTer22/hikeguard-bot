import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape

import structlog
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import Settings
from db.database import Database
from db.queries import KeywordQueries, SpamLogQueries, UserQueries
from services.moderation import _apply_punishment, handle_spam
from services.spam_detector import DetectionResult, SpamDetector
from utils import auto_delete_message, is_admin

logger = structlog.get_logger()
router = Router(name="admin")

ALL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_documents=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_audios=True,
    can_send_voice_notes=True,
    can_send_video_notes=True,
)


SPAM_VOTES_REQUIRED = 5
_VOTE_TTL = 3600  # 1 hour

@dataclass
class SpamVote:
    chat_id: int
    target_message_id: int
    target_user_id: int
    target_username: str | None
    target_full_name: str
    target_text: str | None
    vote_message_id: int
    voters: set[int] = field(default_factory=set)
    created_at: float = field(default_factory=time.monotonic)

# key: "chat_id:target_message_id"
_active_votes: dict[str, SpamVote] = {}


def _vote_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _cleanup_stale_votes() -> None:
    now = time.monotonic()
    stale = [k for k, v in _active_votes.items() if now - v.created_at > _VOTE_TTL]
    for k in stale:
        del _active_votes[k]


async def _check_admin(message: Message, bot: Bot) -> bool:
    if not message.from_user or not message.chat:
        return False
    if message.chat.type == "private":
        return False
    return await is_admin(bot, message.chat.id, message.from_user.id)


def _schedule_delete(bot: Bot, chat_id: int, *message_ids: int, delay: int) -> None:
    """Schedule auto-deletion for one or more messages."""
    for mid in message_ids:
        asyncio.create_task(auto_delete_message(bot, chat_id, mid, delay))


@router.message(Command("chatid"))
async def cmd_chatid(message: Message, bot: Bot) -> None:
    if not await _check_admin(message, bot):
        return
    reply = await message.reply(
        f"Chat ID: <code>{message.chat.id}</code>\n"
        f"Your ID: <code>{message.from_user.id}</code>"
    )
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)


@router.message(Command("trust"))
async def cmd_trust(message: Message, bot: Bot, db: Database) -> None:
    if not await _check_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply(
            "Використання: /trust — відповіддю на повідомлення користувача"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    target = message.reply_to_message.from_user
    users = UserQueries(db)
    await users.upsert_user(
        user_id=target.id, username=target.username, full_name=target.full_name,
    )

    await bot.restrict_chat_member(
        chat_id=message.chat.id,
        user_id=target.id,
        permissions=ALL_PERMISSIONS,
    )
    await users.set_trusted(target.id)

    display = f"@{target.username}" if target.username else f"ID:{target.id}"
    reply = await message.reply(f"✅ {display} тепер довірений учасник")
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
    logger.info(
        "admin_trust_applied",
        target_user_id=target.id,
        by=message.from_user.id,
    )


@router.message(Command("untrust"))
async def cmd_untrust(message: Message, bot: Bot, db: Database) -> None:
    if not await _check_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply(
            "Використання: /untrust — відповіддю на повідомлення користувача"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    target = message.reply_to_message.from_user
    users = UserQueries(db)
    await users.upsert_user(
        user_id=target.id, username=target.username, full_name=target.full_name,
    )
    await users.set_untrusted(target.id)

    display = f"@{target.username}" if target.username else f"ID:{target.id}"
    reply = await message.reply(f"❌ {display} більше не довірений учасник")
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
    logger.info(
        "admin_untrust_applied",
        target_user_id=target.id,
        by=message.from_user.id,
    )


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot) -> None:
    if not await _check_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply(
            "Використання: /mute <хвилин> — відповіддю на повідомлення"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    args = (message.text or "").split(maxsplit=1)
    minutes = 60
    if len(args) >= 2:
        try:
            minutes = int(args[1])
        except ValueError:
            reply = await message.reply("Кількість хвилин повинна бути числом")
            _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
            return

    if minutes < 1 or minutes > 525600:
        reply = await message.reply("Від 1 хвилини до 365 днів (525600)")
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    target = message.reply_to_message.from_user
    until = datetime.now(tz=UTC) + timedelta(minutes=minutes)
    await bot.restrict_chat_member(
        chat_id=message.chat.id,
        user_id=target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until,
    )

    display = f"@{target.username}" if target.username else f"ID:{target.id}"
    reply = await message.reply(f"🔇 {display} замучено на {minutes} хв")
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
    logger.info(
        "admin_mute",
        target_user_id=target.id,
        minutes=minutes,
        by=message.from_user.id,
    )


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot) -> None:
    if not await _check_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply(
            "Використання: /unmute — відповіддю на повідомлення користувача"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    target = message.reply_to_message.from_user
    await bot.restrict_chat_member(
        chat_id=message.chat.id,
        user_id=target.id,
        permissions=ALL_PERMISSIONS,
    )

    display = f"@{target.username}" if target.username else f"ID:{target.id}"
    reply = await message.reply(f"🔊 {display} розмучено")
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
    logger.info(
        "admin_unmute",
        target_user_id=target.id,
        by=message.from_user.id,
    )


@router.message(Command("set_limit"))
async def cmd_set_limit(
    message: Message, bot: Bot, db: Database, config: Settings,
) -> None:
    if not await _check_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply(
            "Використання: /set_limit <мутів> — відповіддю на повідомлення\n"
            "/set_limit reset — повернути до стандартного"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        reply = await message.reply(
            "Використання: /set_limit <мутів> або /set_limit reset"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    target = message.reply_to_message.from_user
    users = UserQueries(db)
    await users.upsert_user(
        user_id=target.id, username=target.username, full_name=target.full_name,
    )
    display = f"@{target.username}" if target.username else f"ID:{target.id}"

    if args[1].strip().lower() == "reset":
        await users.set_ban_on_strike(target.id, None)
        default_mutes = config.ban_on_strike - 1
        reply = await message.reply(
            f"✅ {display} — ліміт скинуто до стандартного ({default_mutes} мут до бану)"
        )
    else:
        try:
            mutes = int(args[1])
        except ValueError:
            reply = await message.reply("Кількість мутів повинна бути числом")
            _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
            return

        if mutes < 1:
            reply = await message.reply("Мінімум 1 мут до бану")
            _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
            return

        await users.set_ban_on_strike(target.id, mutes + 1)
        reply = await message.reply(f"✅ {display} — {mutes} мутів до бану")

    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
    logger.info(
        "admin_set_limit",
        target_user_id=target.id,
        value=args[1],
        by=message.from_user.id,
    )


@router.message(Command("spam"))
async def cmd_spam(
    message: Message, bot: Bot, db: Database, config: Settings,
) -> None:
    if not message.from_user or not message.chat or message.chat.type == "private":
        return

    target_msg = message.reply_to_message
    if not target_msg or not target_msg.from_user:
        reply = await message.reply(
            "Використання: /spam — відповіддю на спам-повідомлення"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    # Don't act on admins
    if await is_admin(bot, message.chat.id, target_msg.from_user.id):
        reply = await message.reply("Не можна застосувати до адміністратора")
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    # Delete the /spam command message itself
    try:
        await message.delete()
    except Exception:
        pass

    # Admin — instant action
    if await is_admin(bot, message.chat.id, message.from_user.id):
        result = DetectionResult(
            is_spam=True,
            score=0,
            method="manual",
            caption_text=target_msg.text or target_msg.caption,
        )
        await handle_spam(target_msg, bot, db, config, result)
        logger.info(
            "admin_manual_spam",
            target_user_id=target_msg.from_user.id,
            by=message.from_user.id,
        )
        return

    # Non-admin — start or join a vote
    # Can't report your own message
    if message.from_user.id == target_msg.from_user.id:
        return

    _cleanup_stale_votes()
    key = _vote_key(message.chat.id, target_msg.message_id)

    if key in _active_votes:
        # Vote already exists — add voter via the existing post
        vote = _active_votes[key]
        if message.from_user.id not in vote.voters:
            vote.voters.add(message.from_user.id)
            count = len(vote.voters)
            await _update_vote_message(bot, vote, count)
            if count >= SPAM_VOTES_REQUIRED:
                del _active_votes[key]
                await _execute_community_spam(bot, db, config, vote)
        return

    # Create new vote
    count = 1
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Спам ({count}/{SPAM_VOTES_REQUIRED})",
            callback_data=f"sv:{message.chat.id}:{target_msg.message_id}",
        )
    ]])
    vote_msg = await target_msg.reply(
        f"🚨 Скарга на спам — {count}/{SPAM_VOTES_REQUIRED} голосів для видалення",
        reply_markup=keyboard,
    )
    vote = SpamVote(
        chat_id=message.chat.id,
        target_message_id=target_msg.message_id,
        target_user_id=target_msg.from_user.id,
        target_username=target_msg.from_user.username,
        target_full_name=target_msg.from_user.full_name,
        target_text=target_msg.text or target_msg.caption,
        vote_message_id=vote_msg.message_id,
        voters={message.from_user.id},
    )
    _active_votes[key] = vote
    logger.info(
        "spam_vote_started",
        target_user_id=target_msg.from_user.id,
        by=message.from_user.id,
    )


@router.callback_query(F.data.startswith("sv:"))
async def on_spam_vote(
    callback: CallbackQuery, bot: Bot, db: Database, config: Settings,
) -> None:
    if not callback.data or not callback.from_user:
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        return

    chat_id, target_msg_id = int(parts[1]), int(parts[2])
    key = _vote_key(chat_id, target_msg_id)
    vote = _active_votes.get(key)

    if not vote:
        await callback.answer("Голосування завершено")
        return

    # Target user can't vote on their own report
    if callback.from_user.id == vote.target_user_id:
        await callback.answer("Ви не можете голосувати за власне повідомлення")
        return

    # Admin click — instant action
    if await is_admin(bot, chat_id, callback.from_user.id):
        del _active_votes[key]
        await callback.answer("Адмін підтвердив — видаляю")
        await _execute_community_spam(bot, db, config, vote)
        return

    # Already voted
    if callback.from_user.id in vote.voters:
        await callback.answer("Ви вже проголосували")
        return

    vote.voters.add(callback.from_user.id)
    count = len(vote.voters)
    await callback.answer(f"Голос враховано: {count}/{SPAM_VOTES_REQUIRED}")

    if count >= SPAM_VOTES_REQUIRED:
        del _active_votes[key]
        await _execute_community_spam(bot, db, config, vote)
    else:
        await _update_vote_message(bot, vote, count)


async def _update_vote_message(bot: Bot, vote: SpamVote, count: int) -> None:
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"Спам ({count}/{SPAM_VOTES_REQUIRED})",
            callback_data=f"sv:{vote.chat_id}:{vote.target_message_id}",
        )
    ]])
    try:
        await bot.edit_message_text(
            chat_id=vote.chat_id,
            message_id=vote.vote_message_id,
            text=f"🚨 Скарга на спам — {count}/{SPAM_VOTES_REQUIRED} голосів для видалення",
            reply_markup=keyboard,
        )
    except Exception:
        pass


async def _execute_community_spam(
    bot: Bot, db: Database, config: Settings, vote: SpamVote,
) -> None:
    # Delete vote message
    try:
        await bot.delete_message(vote.chat_id, vote.vote_message_id)
    except Exception:
        pass

    # Delete target message
    try:
        await bot.delete_message(vote.chat_id, vote.target_message_id)
    except Exception:
        logger.warning("vote_target_delete_failed", message_id=vote.target_message_id)

    # Punish user + log + notify admin
    users = UserQueries(db)
    await users.upsert_user(
        user_id=vote.target_user_id,
        username=vote.target_username,
        full_name=vote.target_full_name,
    )
    strikes = await users.increment_strikes(vote.target_user_id)
    ban_threshold = await users.get_ban_threshold(vote.target_user_id, config.ban_on_strike)

    action, action_text = await _apply_punishment(
        bot, users, config, vote.chat_id, vote.target_user_id, strikes, ban_threshold,
    )

    await SpamLogQueries(db).log_spam(
        user_id=vote.target_user_id,
        chat_id=vote.chat_id,
        message_id=vote.target_message_id,
        detection_method="community_vote",
        caption_text=vote.target_text[:500] if vote.target_text else None,
        spam_score=0,
        gemini_reason=None,
        action_taken=action,
    )

    # Notify admin
    caption_display = escape(vote.target_text[:200]) if vote.target_text else "—"
    name = escape(vote.target_full_name)
    if vote.target_username:
        user_display = f"@{escape(vote.target_username)} ({name}, ID: {vote.target_user_id})"
    else:
        user_display = f"{name} (ID: {vote.target_user_id})"
    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🚫 <b>Спам (голосування учасників)</b>\n"
                f"👤 {user_display}\n"
                f"📝 Текст: <i>{caption_display}</i>\n"
                f"🗳 Голосів: {len(vote.voters)}\n"
                f"⚡ Дія: {action_text}"
            ),
        )
    except Exception:
        logger.warning("admin_vote_notify_failed")

    logger.info(
        "community_vote_spam",
        target_user_id=vote.target_user_id,
        voters=len(vote.voters),
        action=action,
    )


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot, db: Database) -> None:
    if not await _check_admin(message, bot):
        return

    spam_log = SpamLogQueries(db)

    stats_24h = await spam_log.get_stats(hours=24)
    stats_7d = await spam_log.get_stats(hours=168)
    stats_all = await spam_log.get_stats()
    top_methods = await spam_log.get_top_methods()

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
        f"<b>Методи виявлення:</b>\n{methods_text}"
    )

    reply = await message.reply(text)
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=60)


@router.message(Command("spam_words"))
async def cmd_spam_words(message: Message, bot: Bot, db: Database) -> None:
    if not await _check_admin(message, bot):
        return

    keywords = await KeywordQueries(db).get_all_with_details()

    if not keywords:
        reply = await message.reply(
            "Кастомних ключових слів ще немає.\n"
            "Додайте через: /add_word <слово> <бали>"
        )
    else:
        lines = [f"📝 <b>Кастомні ключові слова ({len(keywords)}):</b>\n"]
        for kw_item in keywords:
            lines.append(
                f"  • <code>{escape(kw_item['word'])}</code> — {kw_item['score']} балів"
            )
        reply = await message.reply("\n".join(lines))

    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=60)


@router.message(Command("add_word"))
async def cmd_add_word(
    message: Message, bot: Bot, db: Database, spam_detector: SpamDetector,
) -> None:
    if not await _check_admin(message, bot):
        return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 2:
        reply = await message.reply(
            "Використання: /add_word <слово> [бали]\nБали за замовчуванням: 3"
        )
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    word = args[1].lower()
    score = 3
    if len(args) >= 3:
        try:
            score = int(args[2])
        except ValueError:
            reply = await message.reply("Бали повинні бути числом")
            _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
            return

    await KeywordQueries(db).add(word, score, message.from_user.id)
    await spam_detector.scorer.reload_custom_keywords()

    reply = await message.reply(f"✅ Додано: <code>{escape(word)}</code> — {score} балів")
    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
    logger.info("keyword_added", word=word, score=score, by=message.from_user.id)


@router.message(Command("remove_word"))
async def cmd_remove_word(
    message: Message, bot: Bot, db: Database, spam_detector: SpamDetector,
) -> None:
    if not await _check_admin(message, bot):
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        reply = await message.reply("Використання: /remove_word <слово>")
        _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
        return

    word = args[1].lower()
    removed = await KeywordQueries(db).remove(word)

    if removed:
        await spam_detector.scorer.reload_custom_keywords()
        reply = await message.reply(f"✅ Видалено: <code>{escape(word)}</code>")
        logger.info("keyword_removed", word=word, by=message.from_user.id)
    else:
        reply = await message.reply(
            f"Слово <code>{escape(word)}</code> не знайдено в кастомних"
        )

    _schedule_delete(bot, message.chat.id, reply.message_id, message.message_id, delay=30)
