import json
from datetime import UTC, datetime

from db.database import Database


class UserQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        quarantine_until: datetime | None = None,
    ) -> None:
        await self._db.db.execute(
            """
            INSERT INTO users (user_id, username, full_name, quarantine_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                quarantine_until = COALESCE(excluded.quarantine_until, quarantine_until)
            """,
            (user_id, username, full_name, quarantine_until),
        )
        await self._db.db.commit()

    async def increment_strikes(self, user_id: int) -> int:
        await self._db.db.execute(
            "UPDATE users SET spam_strikes = spam_strikes + 1 WHERE user_id = ?",
            (user_id,),
        )
        await self._db.db.commit()
        cursor = await self._db.db.execute(
            "SELECT spam_strikes FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["spam_strikes"] if row else 1

    async def is_trusted(self, user_id: int) -> bool:
        """User is trusted only if manually marked by admin via /trust."""
        cursor = await self._db.db.execute(
            "SELECT 1 FROM users WHERE user_id = ? AND is_trusted = 1",
            (user_id,),
        )
        return await cursor.fetchone() is not None

    async def set_trusted(self, user_id: int) -> None:
        await self._db.db.execute(
            "UPDATE users SET is_trusted = 1, spam_strikes = 0 WHERE user_id = ?",
            (user_id,),
        )
        await self._db.db.commit()

    async def set_untrusted(self, user_id: int) -> None:
        await self._db.db.execute(
            "UPDATE users SET is_trusted = 0 WHERE user_id = ?",
            (user_id,),
        )
        await self._db.db.commit()

    async def get_ban_threshold(self, user_id: int, default: int) -> int:
        """Per-user ban_on_strike or global default."""
        cursor = await self._db.db.execute(
            "SELECT ban_on_strike FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row and row["ban_on_strike"] is not None:
            return row["ban_on_strike"]
        return default

    async def set_ban_on_strike(self, user_id: int, value: int | None) -> None:
        await self._db.db.execute(
            "UPDATE users SET ban_on_strike = ? WHERE user_id = ?",
            (value, user_id),
        )
        await self._db.db.commit()

    async def is_allowed(self, user_id: int) -> bool:
        """User is allowed — skip all spam checks."""
        cursor = await self._db.db.execute(
            "SELECT 1 FROM users WHERE user_id = ? AND is_allowed = 1",
            (user_id,),
        )
        return await cursor.fetchone() is not None

    async def set_allowed(self, user_id: int) -> None:
        await self._db.db.execute(
            "UPDATE users SET is_allowed = 1 WHERE user_id = ?",
            (user_id,),
        )
        await self._db.db.commit()

    async def set_not_allowed(self, user_id: int) -> None:
        await self._db.db.execute(
            "UPDATE users SET is_allowed = 0 WHERE user_id = ?",
            (user_id,),
        )
        await self._db.db.commit()

    async def set_banned(self, user_id: int) -> None:
        await self._db.db.execute(
            "UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,)
        )
        await self._db.db.commit()


class SpamLogQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def log_spam(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        detection_method: str,
        caption_text: str | None,
        spam_score: float,
        gemini_reason: str | None,
        action_taken: str,
    ) -> None:
        await self._db.db.execute(
            """
            INSERT INTO spam_log
                (user_id, chat_id, message_id, detection_method,
                 caption_text, spam_score, gemini_reason, action_taken)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, chat_id, message_id, detection_method,
                caption_text, spam_score, gemini_reason, action_taken,
            ),
        )
        await self._db.db.commit()

    async def get_stats(self, hours: int | None = None) -> dict:
        if hours is not None:
            cursor = await self._db.db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN action_taken = 'banned' THEN 1 ELSE 0 END) as bans
                FROM spam_log
                WHERE created_at >= datetime('now', ?)
                """,
                (f"-{hours} hours",),
            )
        else:
            cursor = await self._db.db.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN action_taken = 'banned' THEN 1 ELSE 0 END) as bans
                FROM spam_log
                """
            )
        row = await cursor.fetchone()
        return {"total": row["total"] or 0, "bans": row["bans"] or 0}

    async def get_top_methods(self, limit: int = 5) -> list[tuple[str, int]]:
        cursor = await self._db.db.execute(
            """
            SELECT detection_method, COUNT(*) as cnt
            FROM spam_log
            GROUP BY detection_method
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [(row["detection_method"], row["cnt"]) async for row in cursor]


class KeywordQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_all(self) -> dict[str, int]:
        cursor = await self._db.db.execute("SELECT word, score FROM keywords")
        return {row["word"]: row["score"] async for row in cursor}

    async def add(self, word: str, score: int, added_by: int) -> None:
        await self._db.db.execute(
            """
            INSERT INTO keywords (word, score, added_by) VALUES (?, ?, ?)
            ON CONFLICT(word) DO UPDATE SET score = excluded.score
            """,
            (word, score, added_by),
        )
        await self._db.db.commit()

    async def remove(self, word: str) -> bool:
        cursor = await self._db.db.execute(
            "DELETE FROM keywords WHERE word = ?", (word,)
        )
        await self._db.db.commit()
        return cursor.rowcount > 0

    async def get_all_with_details(self) -> list[dict]:
        cursor = await self._db.db.execute(
            "SELECT word, score, added_by, created_at FROM keywords ORDER BY word"
        )
        return [dict(row) async for row in cursor]


class GeminiCacheQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, file_unique_id: str) -> dict | None:
        cursor = await self._db.db.execute(
            "SELECT * FROM gemini_cache WHERE file_unique_id = ?",
            (file_unique_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def save(
        self,
        file_unique_id: str,
        is_spam: bool,
        confidence: float,
        reason: str,
    ) -> None:
        await self._db.db.execute(
            """
            INSERT INTO gemini_cache (file_unique_id, is_spam, confidence, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(file_unique_id) DO UPDATE SET
                is_spam = excluded.is_spam,
                confidence = excluded.confidence,
                reason = excluded.reason
            """,
            (file_unique_id, int(is_spam), confidence, reason),
        )
        await self._db.db.commit()

    async def cleanup(self, days: int = 30) -> int:
        cursor = await self._db.db.execute(
            "DELETE FROM gemini_cache WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await self._db.db.commit()
        return cursor.rowcount


class JoinRequestQueries:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: str | None,
        full_name: str | None,
        score: int,
        signals: dict[str, int],
        decision: str,
        decision_source: str,
        decided_by: int | None = None,
    ) -> None:
        decided_at = datetime.now(tz=UTC) if decision != "pending" else None
        await self._db.db.execute(
            """
            INSERT INTO join_requests
                (user_id, chat_id, username, full_name, score, signals,
                 decision, decision_source, decided_by, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, chat_id, username, full_name, score,
                json.dumps(signals, ensure_ascii=False),
                decision, decision_source, decided_by, decided_at,
            ),
        )
        await self._db.db.commit()

    async def resolve_pending(
        self,
        *,
        user_id: int,
        chat_id: int,
        decision: str,
        decided_by: int,
    ) -> bool:
        """Update the most recent pending entry for (user_id, chat_id).
        Returns True if a row was updated, False otherwise."""
        decided_at = datetime.now(tz=UTC)
        cursor = await self._db.db.execute(
            """
            UPDATE join_requests
            SET decision = ?, decision_source = 'admin',
                decided_by = ?, decided_at = ?
            WHERE id = (
                SELECT id FROM join_requests
                WHERE user_id = ? AND chat_id = ? AND decision = 'pending'
                ORDER BY request_date DESC LIMIT 1
            )
            """,
            (decision, decided_by, decided_at, user_id, chat_id),
        )
        await self._db.db.commit()
        return cursor.rowcount > 0

    async def get_stats(self, hours: int = 24) -> dict:
        cursor = await self._db.db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN decision = 'approve' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN decision = 'decline' THEN 1 ELSE 0 END) AS declined,
                SUM(CASE WHEN decision = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN decision_source = 'raid_mode' THEN 1 ELSE 0 END) AS raid_declined
            FROM join_requests
            WHERE request_date >= datetime('now', ?)
            """,
            (f"-{hours} hours",),
        )
        row = await cursor.fetchone()
        return {
            "total": row["total"] or 0,
            "approved": row["approved"] or 0,
            "declined": row["declined"] or 0,
            "pending": row["pending"] or 0,
            "raid_declined": row["raid_declined"] or 0,
        }

    async def cleanup(self, days: int = 90) -> int:
        """Delete resolved rows older than `days`. Keeps pending rows
        (they're either still awaiting admin action or stuck — handle
        separately if pending counts grow stale).
        Returns the number of rows deleted."""
        cursor = await self._db.db.execute(
            """
            DELETE FROM join_requests
            WHERE request_date < datetime('now', ?)
              AND decision != 'pending'
            """,
            (f"-{days} days",),
        )
        await self._db.db.commit()
        return cursor.rowcount
