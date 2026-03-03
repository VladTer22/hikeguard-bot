import asyncio
import json
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

SPAM_CLASSIFICATION_PROMPT = """\
Проаналізуй це зображення з туристичного (hiking) чату. Визнач, чи це спам.

Типи спаму які потрібно виявити:
1. Оголошення про вакансії та працевлаштування (менеджер, адміністратор, HR, sales тощо)
2. Обіцянки заробітку (суми грошей, зарплата, ставка, бонуси за роботу)
3. Казино, азартні ігри, лотереї ("ви виграли", бонуси казино)
4. Фінансові піраміди, криптоскам, інвестиційні схеми
5. Реклама послуг не пов'язаних з туризмом (косметика, курси, нерухомість тощо)

Це НЕ спам (нормальний контент для hiking-чату):
- Продаж, обмін або пошук туристичного спорядження (намети, рюкзаки, спальники, взуття тощо)
- Обговорення цін на спорядження, порівняння, огляди, gear review
- Посилання на магазини спорядження або маркетплейси (OLX, Prom тощо)
- Скріншоти товарів зі спорядженням
- Туристичні фото (пейзажі, гори, стежки, табори)
- Особисті фото (селфі, група людей на маршруті)
- Скріншоти карт, маршрутів, бронювань, погоди
- Фото їжі, меню, цінників у контексті подорожей
- Документи, квитки, візи

Відповідай ТІЛЬКИ у форматі JSON:
{"is_spam": true/false, "confidence": 0.0-1.0, "reason": "коротке пояснення"}"""


@dataclass
class GeminiResult:
    is_spam: bool
    confidence: float
    reason: str


class GeminiClassifier:
    def __init__(self, api_key: str, model: str, timeout: int = 10) -> None:
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=api_key)
        self._types = types
        self._model = model
        self._timeout = timeout

    async def classify_image(self, image_bytes: bytes) -> GeminiResult | None:
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=[
                        self._types.Part.from_bytes(
                            data=image_bytes, mime_type="image/jpeg",
                        ),
                        SPAM_CLASSIFICATION_PROMPT,
                    ],
                ),
                timeout=self._timeout,
            )

            text = response.text
            if not text:
                logger.warning("gemini_empty_response")
                return None

            data = json.loads(_strip_markdown_fences(text))
            result = GeminiResult(
                is_spam=data["is_spam"],
                confidence=float(data["confidence"]),
                reason=data.get("reason", ""),
            )
            logger.info(
                "gemini_classified",
                is_spam=result.is_spam,
                confidence=result.confidence,
                reason=result.reason,
            )
            return result

        except TimeoutError:
            logger.warning("gemini_timeout", timeout=self._timeout)
            return None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("gemini_parse_error", error=str(e))
            return None
        except Exception as e:
            logger.warning("gemini_request_failed", error=str(e))
            return None


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` wrapping if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    return text.strip()
