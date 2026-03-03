import asyncio
from html import escape

import structlog
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import ChatPermissions, Message

from db.database import Database
from db.queries import KeywordQueries, SpamLogQueries, UserQueries
from services.spam_detector import SpamDetector
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


async def _check_admin(message: Message, bot: Bot) -> bool:
    if not message.from_user or not message.chat:
        return False
    if message.chat.type == "private":
        return False
    return await is_admin(bot, message.chat.id, message.from_user.id)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message) -> None:
    if not message.from_user:
        return
    await message.reply(
        f"Chat ID: <code>{message.chat.id}</code>\n"
        f"Your ID: <code>{message.from_user.id}</code>"
    )


@router.message(Command("trust"))
async def cmd_trust(message: Message, bot: Bot, db: Database) -> None:
    if not await _check_admin(message, bot):
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        reply = await message.reply(
            "Використання: /trust — відповіддю на повідомлення користувача"
        )
        asyncio.create_task(
            auto_delete_message(bot, message.chat.id, reply.message_id, 30)
        )
        return

    target = message.reply_to_message.from_user

    await bot.restrict_chat_member(
        chat_id=message.chat.id,
        user_id=target.id,
        permissions=ALL_PERMISSIONS,
    )
    await UserQueries(db).set_trusted(target.id)

    display = f"@{target.username}" if target.username else f"ID:{target.id}"
    reply = await message.reply(f"✅ {display} тепер довірений учасник")
    asyncio.create_task(
        auto_delete_message(bot, message.chat.id, reply.message_id, 30)
    )
    logger.info(
        "admin_trust_applied",
        target_user_id=target.id,
        by=message.from_user.id,
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
    asyncio.create_task(
        auto_delete_message(bot, message.chat.id, reply.message_id, 60)
    )


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

    asyncio.create_task(
        auto_delete_message(bot, message.chat.id, reply.message_id, 60)
    )


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
        asyncio.create_task(
            auto_delete_message(bot, message.chat.id, reply.message_id, 30)
        )
        return

    word = args[1].lower()
    score = 3
    if len(args) >= 3:
        try:
            score = int(args[2])
        except ValueError:
            reply = await message.reply("Бали повинні бути числом")
            asyncio.create_task(
                auto_delete_message(bot, message.chat.id, reply.message_id, 30)
            )
            return

    await KeywordQueries(db).add(word, score, message.from_user.id)
    await spam_detector.scorer.reload_custom_keywords()

    reply = await message.reply(f"✅ Додано: <code>{escape(word)}</code> — {score} балів")
    asyncio.create_task(
        auto_delete_message(bot, message.chat.id, reply.message_id, 30)
    )
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
        asyncio.create_task(
            auto_delete_message(bot, message.chat.id, reply.message_id, 30)
        )
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

    asyncio.create_task(
        auto_delete_message(bot, message.chat.id, reply.message_id, 30)
    )
