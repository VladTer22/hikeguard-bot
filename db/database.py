from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    quarantine_until TIMESTAMP,
    spam_strikes INTEGER DEFAULT 0,
    is_trusted INTEGER DEFAULT 0,
    is_banned INTEGER DEFAULT 0,
    ban_on_strike INTEGER
);

CREATE TABLE IF NOT EXISTS spam_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chat_id INTEGER,
    message_id INTEGER,
    detection_method TEXT,
    caption_text TEXT,
    spam_score REAL,
    gemini_reason TEXT,
    action_taken TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT UNIQUE NOT NULL,
    score INTEGER NOT NULL DEFAULT 3,
    added_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gemini_cache (
    file_unique_id TEXT PRIMARY KEY,
    is_spam INTEGER,
    confidence REAL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS join_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    request_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score INTEGER,
    signals TEXT,
    decision TEXT,                       -- 'approve' | 'decline' | 'pending'
    decision_source TEXT,                -- 'auto' | 'admin' | 'raid_mode'
    decided_by INTEGER,
    decided_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_join_requests_date ON join_requests(request_date);
CREATE INDEX IF NOT EXISTS idx_join_requests_decision ON join_requests(decision);
CREATE INDEX IF NOT EXISTS idx_join_requests_lookup
    ON join_requests(user_id, chat_id, decision);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._migrate(self._db)
        await self._db.commit()
        logger.info("database_initialized", path=self.db_path)

    @staticmethod
    async def _migrate(db: aiosqlite.Connection) -> None:
        """Add columns that don't exist yet (safe to re-run)."""
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "ban_on_strike" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN ban_on_strike INTEGER")
        if "is_allowed" not in columns:
            await db.execute("ALTER TABLE users ADD COLUMN is_allowed INTEGER DEFAULT 0")

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "Database not initialized. Call init() first."
            raise RuntimeError(msg)
        return self._db

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            logger.info("database_closed")
