from io import BytesIO

import structlog
from aiogram import Bot, F, Router
from aiogram.types import Message, PhotoSize

from config import Settings
from db.database import Database
from services.moderation import handle_channel_spam, handle_spam, notify_admins_uncertain
from services.spam_detector import SpamDetector
from utils import is_admin

logger = structlog.get_logger()
router = Router(name="media")


def _is_channel_message(message: Message) -> bool:
    return message.sender_chat is not None and message.sender_chat.type == "channel"


async def _check_media_spam(
    message: Message,
    bot: Bot,
    db: Database,
    spam_detector: SpamDetector,
    config: Settings,
    photo: PhotoSize,
) -> None:
    file = await bot.get_file(photo.file_id)
    if not file.file_path:
        return

    bio = BytesIO()
    await bot.download_file(file.file_path, bio)

    result = await spam_detector.check_photo(
        message=message,
        image_bytes=bio.getvalue(),
        file_unique_id=photo.file_unique_id,
    )

    is_channel = _is_channel_message(message)

    if result.is_spam:
        if is_channel:
            await handle_channel_spam(message, bot, db, config, result)
        else:
            await handle_spam(message, bot, db, config, result)
    elif result.flag_for_admin and not is_channel:
        await notify_admins_uncertain(message, bot, config, result)


def _should_skip(message: Message) -> bool:
    """Return True if the message should not be checked for spam."""
    if not message.chat:
        return True
    if message.chat.type == "private":
        return True
    return False


@router.message(F.photo)
async def handle_photo(
    message: Message,
    bot: Bot,
    db: Database,
    spam_detector: SpamDetector,
    config: Settings,
) -> None:
    if _should_skip(message):
        return

    if not _is_channel_message(message):
        if not message.from_user:
            return
        if await is_admin(bot, message.chat.id, message.from_user.id):
            return

    await _check_media_spam(message, bot, db, spam_detector, config, message.photo[-1])


@router.message(F.animation)
async def handle_animation(
    message: Message,
    bot: Bot,
    db: Database,
    spam_detector: SpamDetector,
    config: Settings,
) -> None:
    if _should_skip(message):
        return

    if not _is_channel_message(message):
        if not message.from_user:
            return
        if await is_admin(bot, message.chat.id, message.from_user.id):
            return

    if not message.animation or not message.animation.thumbnail:
        return

    await _check_media_spam(
        message, bot, db, spam_detector, config, message.animation.thumbnail,
    )
