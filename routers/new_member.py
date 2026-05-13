import time
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated, ChatPermissions, User

from config import Settings
from db.database import Database
from db.queries import SpamLogQueries, UserQueries
from services.cas import CASChecker
from services.join_scorer import score_profile
from services.velocity_tracker import VelocityTracker
from utils import format_user

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

# Per-chat sliding-window tracker for chat_member events. Independent from
# the join_request tracker — direct joins and approval-link joins are
# separate attack vectors and don't need shared accounting.
_tracker: VelocityTracker | None = None
_raid_announcements: dict[int, float] = {}


def _get_tracker(config: Settings) -> VelocityTracker:
    global _tracker
    if _tracker is None:
        _tracker = VelocityTracker(
            threshold=config.raid_threshold,
            window_sec=config.raid_window_sec,
            raid_minutes=config.raid_mode_minutes,
        )
    return _tracker


async def _record_ban(
    db: Database,
    *,
    user: User,
    chat_id: int,
    detection_method: str,
    score: int,
) -> None:
    """Persist a member-gate ban to users + spam_log. Swallows DB errors."""
    try:
        users = UserQueries(db)
        await users.upsert_user(
            user_id=user.id, username=user.username, full_name=user.full_name,
        )
        await users.set_banned(user.id)
        await SpamLogQueries(db).log_spam(
            user_id=user.id,
            chat_id=chat_id,
            message_id=0,
            detection_method=detection_method,
            caption_text=None,
            spam_score=score,
            gemini_reason=None,
            action_taken="banned",
        )
    except Exception as e:
        logger.error(
            "member_ban_record_failed",
            user_id=user.id, detection_method=detection_method, error=str(e),
        )


async def _announce_member_raid(
    bot: Bot, config: Settings, chat_id: int, now: float,
) -> None:
    last = _raid_announcements.get(chat_id, 0)
    if now - last < config.raid_announce_interval_sec:
        return
    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=(
                f"🚨 <b>Виявлено флуд вступів у чат</b>\n"
                f"Чат: <code>{chat_id}</code>\n"
                f"Поріг: {config.raid_threshold}/{config.raid_window_sec}с\n"
                f"Авто-бан нових на {config.raid_mode_minutes} хв."
            ),
        )
        _raid_announcements[chat_id] = now
    except Exception:
        logger.warning("member_raid_announce_failed", chat_id=chat_id)


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
    if old.status in ("member", "administrator", "creator", "restricted"):
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

    # Layer 0.5: Member gate — velocity + score-based instant ban.
    # For public chats where bots bypass the approval-link gate via direct join.
    if config.member_gate_enabled:
        tracker = _get_tracker(config)
        now = time.monotonic()
        tracker.record(chat_id=chat_id, ts=now)

        if tracker.in_raid_mode(chat_id=chat_id, now=now):
            await _announce_member_raid(bot, config, chat_id, now)
            try:
                await bot.ban_chat_member(
                    chat_id=chat_id, user_id=user.id, revoke_messages=True,
                )
            except Exception as e:
                logger.warning(
                    "member_raid_ban_failed", user_id=user.id, error=str(e),
                )
                return
            await _record_ban(
                db, user=user, chat_id=chat_id,
                detection_method="join_raid", score=0,
            )
            logger.info("member_raid_ban", user_id=user.id, chat_id=chat_id)
            return

        result = score_profile(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            is_premium=bool(user.is_premium),
            cas_hit=False,  # CAS already checked above; reached here = not banned
        )
        if result.score >= config.member_auto_ban_score:
            try:
                await bot.ban_chat_member(
                    chat_id=chat_id, user_id=user.id, revoke_messages=True,
                )
            except Exception as e:
                logger.warning(
                    "member_score_ban_failed", user_id=user.id, error=str(e),
                )
                return
            await _record_ban(
                db, user=user, chat_id=chat_id,
                detection_method="join_score", score=result.score,
            )
            signals_text = ", ".join(
                f"{k}({v:+d})" for k, v in result.signals.items()
            ) or "—"
            try:
                await bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=(
                        f"🚫 <b>Авто-бан на вході (score)</b>\n"
                        f"👤 {format_user(user.id, user.username, user.full_name)}\n"
                        f"Оцінка: <b>{result.score}</b> "
                        f"(поріг ≥ {config.member_auto_ban_score})\n"
                        f"Сигнали: <i>{signals_text}</i>"
                    ),
                )
            except Exception:
                logger.warning("member_score_ban_notify_failed", user_id=user.id)
            logger.info(
                "member_score_ban",
                user_id=user.id, score=result.score, signals=result.signals,
            )
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
