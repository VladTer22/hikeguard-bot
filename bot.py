import asyncio
import sys

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import settings
from db.database import Database
from db.queries import GeminiCacheQueries
from middlewares.throttle import ThrottleMiddleware
from routers import admin, media, new_member, text
from services.cas import CASChecker
from services.gemini import GeminiClassifier
from services.keyword_scorer import KeywordScorer
from services.spam_detector import SpamDetector

logger = structlog.get_logger()


def setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def main() -> None:
    setup_logging()

    db = Database(settings.db_path)
    await db.init()

    keyword_scorer = KeywordScorer(db)
    await keyword_scorer.reload_custom_keywords()

    removed = await GeminiCacheQueries(db).cleanup()
    if removed:
        logger.info("gemini_cache_cleaned", removed=removed)

    cas_checker = CASChecker()

    gemini: GeminiClassifier | None = None
    if settings.gemini_enabled:
        gemini = GeminiClassifier(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            timeout=settings.gemini_timeout,
        )
        logger.info("gemini_enabled", model=settings.gemini_model)
    else:
        logger.warning("gemini_disabled", reason="GEMINI_API_KEY not set")

    spam_detector = SpamDetector(
        scorer=keyword_scorer,
        gemini=gemini,
        db=db,
        config=settings,
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp["db"] = db
    dp["spam_detector"] = spam_detector
    dp["cas_checker"] = cas_checker
    dp["config"] = settings

    dp.message.middleware(ThrottleMiddleware())

    dp.include_router(admin.router)
    dp.include_router(new_member.router)
    dp.include_router(media.router)
    dp.include_router(text.router)

    logger.info("bot_starting", admin_chat_id=settings.admin_chat_id)

    try:
        await dp.start_polling(bot)
    finally:
        logger.info("bot_shutting_down")
        await cas_checker.close()
        await db.close()
        logger.info("bot_shutdown_complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
