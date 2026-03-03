import structlog
from aiogram import Bot, F, Router
from aiogram.types import Message

from config import Settings
from db.database import Database
from services.moderation import handle_spam
from services.spam_detector import DetectionResult, SpamDetector
from utils import is_admin

logger = structlog.get_logger()
router = Router(name="text")


@router.message(F.text, ~F.text.startswith("/"))
async def handle_text(
    message: Message,
    bot: Bot,
    db: Database,
    spam_detector: SpamDetector,
    config: Settings,
) -> None:
    if not message.from_user or not message.chat:
        return

    if message.chat.type == "private":
        return

    if await is_admin(bot, message.chat.id, message.from_user.id):
        return

    scoring = spam_detector.scorer.calculate_score(message.text or "")

    if scoring.total_score >= config.spam_threshold:
        logger.info(
            "spam_detected",
            user_id=message.from_user.id,
            method="text_keywords",
            score=scoring.total_score,
            keywords=[k for k, _ in scoring.matched_keywords],
        )
        result = DetectionResult(
            is_spam=True,
            score=scoring.total_score,
            method="text_keywords",
            caption_text=message.text,
            matched_keywords=scoring.matched_keywords,
            matched_patterns=scoring.matched_patterns,
        )
        await handle_spam(message, bot, db, config, result)
