import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated, ChatPermissions

from config import Settings
from db.database import Database
from db.queries import UserQueries
from services.cas import CASChecker
from utils import auto_delete_message, format_user

logger = structlog.get_logger()
router = Router(name="new_member")

QUARANTINE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_photos=False,
    can_send_videos=False,
    can_send_documents=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_send_audios=False,
    can_send_voice_notes=False,
    can_send_video_notes=False,
)

WELCOME_TEXT = (
    "Ласкаво просимо! 👋\n"
    "Для захисту від спаму нові учасники можуть відправляти "
    "тільки текстові повідомлення протягом перших {hours} годин.\n"
    "Фото, відео та стікери стануть доступні автоматично."
)


@router.chat_member()
async def on_chat_member_update(
    event: ChatMemberUpdated,
    bot: Bot,
    db: Database,
    cas_checker: CASChecker,
    config: Settings,
) -> None:
    old = event.old_chat_member
    new = event.new_chat_member
    if old.status in ("member", "administrator", "creator"):
        return
    if new.status not in ("member", "restricted"):
        return

    user = new.user

    bot_info = await bot.me()
    if user.id == bot_info.id:
        return

    chat_id = event.chat.id

    logger.info(
        "new_member_joined",
        user_id=user.id,
        username=user.username,
        chat_id=chat_id,
    )

    # Layer 0: CAS check
    if await cas_checker.is_banned(user.id):
        await bot.ban_chat_member(chat_id=chat_id, user_id=user.id, revoke_messages=True)
        logger.info("cas_ban_applied", user_id=user.id, username=user.username)

        users = UserQueries(db)
        await users.upsert_user(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )
        await users.set_banned(user.id)

        try:
            await bot.send_message(
                chat_id=config.admin_chat_id,
                text=(
                    f"🚫 <b>CAS бан</b>\n"
                    f"👤 {format_user(user.id, user.username, user.full_name)}\n"
                    f"Знайдено в базі CAS — автоматичний бан."
                ),
            )
        except Exception:
            logger.warning("admin_cas_notify_failed")
        return

    # Layer 1: Quarantine
    quarantine_until = datetime.now(tz=UTC) + timedelta(hours=config.quarantine_hours)

    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user.id,
        permissions=QUARANTINE_PERMISSIONS,
        until_date=quarantine_until,
    )

    users = UserQueries(db)
    await users.upsert_user(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        quarantine_until=quarantine_until,
    )

    logger.info(
        "quarantine_applied",
        user_id=user.id,
        until=quarantine_until.isoformat(),
    )

    welcome = await bot.send_message(
        chat_id=chat_id,
        text=WELCOME_TEXT.format(hours=config.quarantine_hours),
    )
    asyncio.create_task(
        auto_delete_message(bot, chat_id, welcome.message_id, config.auto_delete_service_sec)
    )
