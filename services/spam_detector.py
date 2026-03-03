from dataclasses import dataclass, field

import structlog
from aiogram.types import Message

from config import Settings
from db.database import Database
from db.queries import GeminiCacheQueries, UserQueries
from services.gemini import GeminiClassifier, GeminiResult
from services.keyword_scorer import KeywordScorer, ScoringResult

logger = structlog.get_logger()


@dataclass
class DetectionResult:
    is_spam: bool
    score: float
    method: str  # 'caption_keywords', 'text_keywords', 'gemini', 'cas'
    caption_text: str | None = None
    matched_keywords: list[tuple[str, int]] = field(default_factory=list)
    matched_patterns: list[tuple[str, int]] = field(default_factory=list)
    gemini_result: GeminiResult | None = None
    flag_for_admin: bool = False


class SpamDetector:
    def __init__(
        self,
        scorer: KeywordScorer,
        gemini: GeminiClassifier | None,
        db: Database,
        config: Settings,
    ) -> None:
        self.scorer = scorer
        self.gemini = gemini
        self._users = UserQueries(db)
        self._gemini_cache = GeminiCacheQueries(db)
        self._config = config

    async def check_photo(
        self,
        message: Message,
        image_bytes: bytes,
        file_unique_id: str,
    ) -> DetectionResult:
        """Cascade: caption keywords → trusted-user shortcut → Gemini vision."""
        caption = message.caption or ""
        user_id = message.from_user.id if message.from_user else 0

        # Step 1: Caption keyword scoring (free, instant)
        scoring = self.scorer.calculate_score(caption) if caption else ScoringResult()

        if scoring.total_score >= self._config.spam_threshold:
            logger.info(
                "spam_detected",
                method="caption_keywords",
                score=scoring.total_score,
                user_id=user_id,
            )
            return DetectionResult(
                is_spam=True,
                score=scoring.total_score,
                method="caption_keywords",
                caption_text=caption,
                matched_keywords=scoring.matched_keywords,
                matched_patterns=scoring.matched_patterns,
            )

        # Step 2: Skip Gemini for trusted users with no keyword signals
        if scoring.total_score == 0 and user_id and await self._users.is_trusted(user_id):
            logger.debug("skipping_gemini_trusted_user", user_id=user_id)
            return DetectionResult(is_spam=False, score=0, method="none")

        # Step 3: Gemini Vision
        gemini_result = await self._classify_with_gemini(image_bytes, file_unique_id)

        if gemini_result and gemini_result.is_spam:
            if gemini_result.confidence >= 0.7:
                return self._build_result(
                    is_spam=True, scoring=scoring, caption=caption,
                    gemini_result=gemini_result,
                )
            return self._build_result(
                is_spam=False, scoring=scoring, caption=caption,
                gemini_result=gemini_result, flag_for_admin=True,
            )

        return self._build_result(
            is_spam=False, scoring=scoring, caption=caption, gemini_result=gemini_result,
        )

    async def _classify_with_gemini(
        self,
        image_bytes: bytes,
        file_unique_id: str,
    ) -> GeminiResult | None:
        if not self.gemini:
            return None

        cached = await self._gemini_cache.get(file_unique_id)
        if cached:
            logger.info("gemini_cache_hit", file_unique_id=file_unique_id)
            return GeminiResult(
                is_spam=bool(cached["is_spam"]),
                confidence=cached["confidence"],
                reason=cached["reason"],
            )

        result = await self.gemini.classify_image(image_bytes)

        if result:
            await self._gemini_cache.save(
                file_unique_id=file_unique_id,
                is_spam=result.is_spam,
                confidence=result.confidence,
                reason=result.reason,
            )

        return result

    @staticmethod
    def _build_result(
        *,
        is_spam: bool,
        scoring: ScoringResult,
        caption: str,
        gemini_result: GeminiResult | None = None,
        flag_for_admin: bool = False,
    ) -> DetectionResult:
        return DetectionResult(
            is_spam=is_spam,
            score=scoring.total_score,
            method="gemini" if gemini_result else "none",
            caption_text=caption or None,
            matched_keywords=scoring.matched_keywords,
            matched_patterns=scoring.matched_patterns,
            gemini_result=gemini_result,
            flag_for_admin=flag_for_admin,
        )
