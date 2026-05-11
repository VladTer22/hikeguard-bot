import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape

import structlog
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db.database import Database
from db.queries import UserQueries
from utils import auto_delete_message, is_admin

logger = structlog.get_logger()
router = Router(name="purge")

_PURGE_TTL = 300
_BAN_DELAY = 0.1
_PROGRESS_INTERVAL = 5.0


@dataclass
class PendingPurge:
    chat_id: int
    initiated_by: int
    start: datetime
    end: datetime
    user_ids: list[int]
    created_at: float = field(default_factory=time.monotonic)


_pending: dict[str, PendingPurge] = {}


def _cleanup_stale() -> None:
    now = time.monotonic()
    stale = [k for k, v in _pending.items() if now - v.created_at > _PURGE_TTL]
    for k in stale:
        del _pending[k]


def _parse_args(args: list[str]) -> tuple[datetime, datetime] | None:
    """Parse args into (start, end) UTC datetimes. Returns None on invalid input."""
    now = datetime.now(tz=UTC)

    if len(args) == 1:
        try:
            minutes = int(args[0])
        except ValueError:
            return None
        if minutes < 1 or minutes > 43200:
            return None
        return now - timedelta(minutes=minutes), now

    if len(args) == 2:
        try:
            start_t = datetime.strptime(args[0], "%H:%M").time()
            end_t = datetime.strptime(args[1], "%H:%M").time()
        except ValueError:
            return None
        today = now.date()
        start = datetime.combine(today, start_t, tzinfo=UTC)
        end = datetime.combine(today, end_t, tzinfo=UTC)
        if end <= start:
            return None
        return start, end

    return None


def _format_preview(rows: list, start: datetime, end: datetime) -> str:
    head = rows[: min(3, len(rows))]
    tail_start = max(3, len(rows) - 3)
    tail = rows[tail_start:] if len(rows) > 6 else []

    lines = []
    for r in head:
        uname = f"@{escape(r['username'])}" if r["username"] else f"ID {r['user_id']}"
        lines.append(f"  • {uname} — {r['join_date']}")
    if len(rows) > 6:
        lines.append(f"  … +{len(rows) - 6} ще …")
    for r in tail:
        uname = f"@{escape(r['username'])}" if r["username"] else f"ID {r['user_id']}"
        lines.append(f"  • {uname} — {r['join_date']}")

    return (
        f"🧹 <b>Підготовка зачистки</b>\n"
        f"Вікно: <code>{start.strftime('%Y-%m-%d %H:%M')}</code> — "
        f"<code>{end.strftime('%H:%M')}</code> UTC\n"
        f"Кандидатів на бан: <b>{len(rows)}</b>\n\n"
        + "\n".join(lines)
        + "\n\nПідтверди протягом 5 хв."
    )


@router.message(Command("purge_joins"))
async def cmd_purge_joins(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user or not message.chat or message.chat.type == "private":
        return
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        reply = await message.reply(
            "Використання:\n"
            "  /purge_joins &lt;хвилин&gt; — забанити тих, хто приєднався за останні N хв\n"
            "  /purge_joins &lt;HH:MM&gt; &lt;HH:MM&gt; — UTC діапазон сьогодні\n\n"
            "Бан виключає: вже забанених, довірених (/trust), allow-all."
        )
        asyncio.create_task(auto_delete_message(bot, message.chat.id, reply.message_id, 60))
        asyncio.create_task(auto_delete_message(bot, message.chat.id, message.message_id, 60))
        return

    parsed = _parse_args(parts[1:])
    if parsed is None:
        reply = await message.reply(
            "Не вдалось розпарсити аргументи. Приклади:\n"
            "  /purge_joins 60\n"
            "  /purge_joins 13:20 13:24"
        )
        asyncio.create_task(auto_delete_message(bot, message.chat.id, reply.message_id, 60))
        asyncio.create_task(auto_delete_message(bot, message.chat.id, message.message_id, 60))
        return

    start, end = parsed
    cursor = await db.db.execute(
        """
        SELECT user_id, username, full_name, join_date
        FROM users
        WHERE join_date >= ? AND join_date < ?
          AND COALESCE(is_banned, 0) = 0
          AND COALESCE(is_trusted, 0) = 0
          AND COALESCE(is_allowed, 0) = 0
        ORDER BY join_date, user_id
        """,
        (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
    )
    rows = await cursor.fetchall()

    if not rows:
        reply = await message.reply(
            f"Нікого не знайдено у вікні "
            f"<code>{start.strftime('%Y-%m-%d %H:%M')}</code> — "
            f"<code>{end.strftime('%H:%M')}</code> UTC "
            "(не забанених, не довірених)."
        )
        asyncio.create_task(auto_delete_message(bot, message.chat.id, reply.message_id, 60))
        asyncio.create_task(auto_delete_message(bot, message.chat.id, message.message_id, 60))
        return

    _cleanup_stale()
    purge_id = secrets.token_urlsafe(6)
    _pending[purge_id] = PendingPurge(
        chat_id=message.chat.id,
        initiated_by=message.from_user.id,
        start=start,
        end=end,
        user_ids=[r["user_id"] for r in rows],
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"🚫 Забанити {len(rows)}",
                callback_data=f"pgc:{purge_id}",
            ),
            InlineKeyboardButton(text="❌ Скасувати", callback_data=f"pgx:{purge_id}"),
        ]]
    )

    await message.reply(_format_preview(rows, start, end), reply_markup=kb)
    logger.info(
        "purge_preview",
        purge_id=purge_id,
        chat_id=message.chat.id,
        candidates=len(rows),
        start=start.isoformat(),
        end=end.isoformat(),
        by=message.from_user.id,
    )


@router.callback_query(F.data.startswith("pgc:"))
async def on_purge_confirm(
    callback: CallbackQuery, bot: Bot, db: Database,
) -> None:
    if not callback.data or not callback.from_user or not callback.message:
        return
    purge_id = callback.data.split(":", 1)[1]
    purge = _pending.get(purge_id)
    if not purge:
        await callback.answer("Сесія зачистки прострочена")
        return
    if not await is_admin(bot, purge.chat_id, callback.from_user.id):
        await callback.answer("Тільки адмін може підтвердити")
        return

    del _pending[purge_id]
    await callback.answer(f"Запущено бан {len(purge.user_ids)} юзерів")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    asyncio.create_task(_run_purge(bot, db, purge, callback.from_user.id))


@router.callback_query(F.data.startswith("pgx:"))
async def on_purge_cancel(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.data or not callback.from_user or not callback.message:
        return
    purge_id = callback.data.split(":", 1)[1]
    purge = _pending.get(purge_id)
    if not purge:
        await callback.answer("Сесія прострочена")
        return
    if not await is_admin(bot, purge.chat_id, callback.from_user.id):
        await callback.answer("Тільки адмін може скасувати")
        return
    del _pending[purge_id]
    await callback.answer("Скасовано")
    try:
        await callback.message.delete()
    except Exception:
        pass


async def _run_purge(
    bot: Bot, db: Database, purge: PendingPurge, by_user: int,
) -> None:
    users = UserQueries(db)
    banned = 0
    failed = 0
    skipped_admin = 0
    total = len(purge.user_ids)
    last_progress = time.monotonic()

    status = await bot.send_message(
        purge.chat_id,
        f"🧹 Бан {total} юзерів запущено…",
    )

    for idx, uid in enumerate(purge.user_ids, 1):
        try:
            member = await bot.get_chat_member(purge.chat_id, uid)
            if member.status in ("creator", "administrator"):
                skipped_admin += 1
                await asyncio.sleep(_BAN_DELAY)
                continue
        except Exception:
            pass

        try:
            await bot.ban_chat_member(
                purge.chat_id, uid, revoke_messages=True,
            )
            await users.set_banned(uid)
            banned += 1
        except TelegramRetryAfter as e:
            logger.warning("purge_flood_wait", retry_after=e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.ban_chat_member(
                    purge.chat_id, uid, revoke_messages=True,
                )
                await users.set_banned(uid)
                banned += 1
            except Exception as e2:
                failed += 1
                logger.debug("purge_ban_failed", user_id=uid, error=str(e2))
        except Exception as e:
            failed += 1
            logger.debug("purge_ban_failed", user_id=uid, error=str(e))

        await asyncio.sleep(_BAN_DELAY)

        if time.monotonic() - last_progress >= _PROGRESS_INTERVAL:
            last_progress = time.monotonic()
            try:
                await bot.edit_message_text(
                    chat_id=purge.chat_id,
                    message_id=status.message_id,
                    text=(
                        f"🧹 Прогрес: {idx}/{total} "
                        f"({banned} банів, {failed} помилок)"
                    ),
                )
            except Exception:
                pass

    final_text = (
        f"✅ <b>Зачистку завершено</b>\n"
        f"Вікно: <code>{purge.start.strftime('%Y-%m-%d %H:%M')}</code> — "
        f"<code>{purge.end.strftime('%H:%M')}</code> UTC\n"
        f"Забанено: <b>{banned}</b>\n"
        f"Помилок: {failed}\n"
        f"Пропущено адмінів: {skipped_admin}"
    )
    try:
        await bot.edit_message_text(
            chat_id=purge.chat_id,
            message_id=status.message_id,
            text=final_text,
        )
    except Exception:
        await bot.send_message(purge.chat_id, final_text)

    asyncio.create_task(
        auto_delete_message(bot, purge.chat_id, status.message_id, 600)
    )
    logger.info(
        "purge_completed",
        chat_id=purge.chat_id,
        banned=banned,
        failed=failed,
        skipped_admin=skipped_admin,
        by=by_user,
    )
