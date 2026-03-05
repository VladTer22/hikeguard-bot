import re
from dataclasses import dataclass, field

from db.database import Database
from db.queries import KeywordQueries

BUILTIN_KEYWORDS: dict[str, int] = {
    # --- Вакансії (укр) ---
    # Стеми замість повних слів — ловлять усі відмінки:
    # "вакансі" → вакансія, вакансію, вакансії, вакансій
    "вакансі": 4,
    "робота за кордоном": 5,
    # "підробіт" → підробіток, підробітку, підробітком
    "підробіт": 3,
    "набираємо": 3,
    "офіційне працевлаштування": 3,
    "легальна робота": 3,
    # "менеджер з продаж" → продажів, продажу, продажам
    "менеджер з продаж": 4,
    "менеджера з продаж": 4,
    "розширяємо команду": 3,
    "хочеш працювати з дому": 4,
    "навчаємо з нуля": 3,
    "стабільний дохід": 2,
    "кар'єрний ріст": 2,
    # Контакт — широкі стеми (ловлять пиши/пишіть/напишіть + в/у)
    "в особисті": 3,
    "у особисті": 3,
    "в лс": 3,
    "в личк": 3,
    "напишіть мені": 2,
    "віддалено": 2,
    "гнучкий графік": 2,
    "оплата щоденна": 4,
    "досвід не потрібен": 4,
    "без досвіду": 3,
    "безкоштовне житло": 4,
    "ми пропонуємо": 2,
    "ми шукаємо": 2,
    "умови роботи": 2,
    "не зволікай": 2,
    "твої завдання": 2,
    "твої обов'язки": 2,
    "зп від": 2,
    # --- Вакансії (рус) ---
    # "ваканси" → вакансия, вакансию, вакансии, вакансий
    "ваканси": 4,
    "требуются": 3,
    "требуется": 3,
    "работа за рубежом": 5,
    "жилье предоставляется": 4,
    "опыт не нужен": 4,
    # "менеджер по продаж" → продажам, продажи, продаж
    "менеджер по продаж": 4,
    "менеджера по продаж": 4,
    "расширяем команду": 3,
    # ё і е варіанти — спамери пишуть і так, і так
    "удалённая работа": 3,
    "удаленная работа": 3,
    "полностью удалённая": 3,
    "полностью удаленная": 3,
    "стабильный доход": 2,
    "карьерный рост": 2,
    "обучение на старте": 3,
    "в личные": 3,
    "в личку": 3,
    "можно без опыта": 4,
    "без опыта": 3,
    "мы в поиске": 3,
    "мы ищем": 2,
    "хватит скроллить": 2,
    "хочешь работать на чиле": 3,
    "мы предлагаем": 2,
    "условия работы": 2,
    "hr-менеджер": 3,
    "hr менеджер": 3,
    "твои задачи": 2,
    "удаленно": 2,
    "зп от": 2,
    # --- Вакансії (англ) ---
    "sale manager": 3,
    "sales manager": 3,
    "hr-manager": 3,
    "hr manager": 3,
    "we are hiring": 4,
    "напишите мне": 2,
    # --- Крипто/P2P скам ---
    # "продаєт" → продаєте, продаёте, продаєш
    "продаєт": 2,
    "продаёт": 2,
    "продаете": 2,
    # "купуєт" → купуєте, купуєш
    "купуєт": 2,
    "купуете": 2,
    "p2p": 2,
    "usdt": 3,
    "юсдт": 3,
    "trc20": 3,
    "erc20": 3,
    # "бірж" → біржі, біржа, біржу
    "бірж": 2,
    # "бирж" → биржи, биржа, биржу
    "бирж": 2,
    # "схем" → схеми, схема, схему
    "схем": 2,
    "безкоштовно": 2,
    "купівля / продаж": 3,
    "купівля/продаж": 3,
    # --- Казино/скам ---
    "вы выиграли": 5,
    "поздравляем": 2,
    "общий бонус": 4,
    # --- Низький score (часткові сигнали) ---
    # Стеми для відмінків:
    # "бонус" → бонуси, бонусы, бонусів, бонусами
    "бонус": 1,
    # "ставк" → ставка, ставку, ставки
    "ставк": 2,
    # "зарплат" → зарплата, зарплату, зарплати
    "зарплат": 2,
    # "заробітн" → заробітна плата, заробітну плату, заробітної плати
    "заробітн": 2,
    "оплата": 1,
}

REGEX_PATTERNS: list[tuple[str, int, str]] = [
    (r"[\+]?[0-9\s\-\(\)]{10,15}", 2, "phone_number"),
    (r"\d[\d\s]*\s*[\$\€\£₴]", 1, "money_amount"),
    (r"[\$\€\£₴]\s*\d[\d\s]*", 1, "money_amount"),
    (r"\d[\d\s]*\s*(?:грн|uah|usd|eur)", 1, "money_amount"),
    # "графік: 5/2", "графік 5/2", "график:5/2" тощо
    (r"(?:графік|график)\s*:?\s*\d\s*/\s*\d", 2, "work_schedule"),
    (r"\d\/\d\s*,?\s*\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}", 1, "work_schedule"),
    (r"@[a-zA-Z_]\w{4,}", 2, "telegram_username"),
    # Telegram invite links — майже завжди спам в груповому чаті
    (r"t\.me/\+[a-zA-Z0-9_-]+", 5, "telegram_invite"),
    (r"t\.me/joinchat/[a-zA-Z0-9_-]+", 5, "telegram_invite"),
    (r"https?://\S+", 2, "url_link"),
    (r"(?:infinityfree|freehosting|000webhostapp)\.\w+", 5, "scam_domain"),
]

_COMPILED_PATTERNS = [
    (re.compile(p, re.IGNORECASE), score, desc)
    for p, score, desc in REGEX_PATTERNS
]


@dataclass
class ScoringResult:
    total_score: int = 0
    matched_keywords: list[tuple[str, int]] = field(default_factory=list)
    matched_patterns: list[tuple[str, int]] = field(default_factory=list)


class KeywordScorer:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._custom_keywords: dict[str, int] = {}
        self._sorted_keywords: list[tuple[str, int]] = []
        self._refresh_keywords()

    def _refresh_keywords(self) -> None:
        """Merge builtin + custom, sort by length descending for greedy matching."""
        merged = {**BUILTIN_KEYWORDS, **self._custom_keywords}
        self._sorted_keywords = sorted(
            merged.items(), key=lambda kv: len(kv[0]), reverse=True,
        )

    async def reload_custom_keywords(self) -> None:
        self._custom_keywords = await KeywordQueries(self._db).get_all()
        self._refresh_keywords()

    def calculate_score(self, text: str) -> ScoringResult:
        result = ScoringResult()
        if not text:
            return result

        normalized = text.lower()
        matched_ranges: list[tuple[int, int]] = []

        # Keyword matching (longer phrases first to avoid double-counting)
        for keyword, score in self._sorted_keywords:
            start = 0
            while True:
                idx = normalized.find(keyword, start)
                if idx == -1:
                    break
                end = idx + len(keyword)
                overlaps = any(
                    not (end <= mr_start or idx >= mr_end)
                    for mr_start, mr_end in matched_ranges
                )
                if not overlaps:
                    result.matched_keywords.append((keyword, score))
                    result.total_score += score
                    matched_ranges.append((idx, end))
                start = end

        # Regex patterns (one match per pattern type)
        seen_types: set[str] = set()
        for pattern, score, desc in _COMPILED_PATTERNS:
            if desc not in seen_types and pattern.search(normalized):
                result.matched_patterns.append((desc, score))
                result.total_score += score
                seen_types.add(desc)

        return result
