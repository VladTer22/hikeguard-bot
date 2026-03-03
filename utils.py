import asyncio
import contextlib
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest


async def auto_delete_message(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    await asyncio.sleep(delay)
    with contextlib.suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("creator", "administrator")
    except TelegramBadRequest:
        return False


def format_user(user_id: int, username: str | None, full_name: str | None) -> str:
    """Format user for admin log messages. HTML-safe."""
    name = escape(full_name or "Unknown")
    if username:
        return f"@{escape(username)} ({name}, ID: {user_id})"
    return f"{name} (ID: {user_id})"
