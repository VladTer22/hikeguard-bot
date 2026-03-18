import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message

_CLEANUP_INTERVAL = 300  # prune stale entries every 5 minutes
_STALE_THRESHOLD = 60  # entries older than 60 seconds


class ThrottleMiddleware(BaseMiddleware):
    """Anti-flood: ignore messages from the same user faster than rate_limit."""

    def __init__(self, rate_limit: float = 1.0) -> None:
        self._rate_limit = rate_limit
        self._timestamps: dict[int, float] = {}
        self._last_cleanup = time.monotonic()

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if not event.from_user:
            return await handler(event, data)

        # Don't throttle group messages — must check every message for spam
        if event.chat and event.chat.type != "private":
            return await handler(event, data)

        now = time.monotonic()
        user_id = event.from_user.id

        if now - self._timestamps.get(user_id, 0.0) < self._rate_limit:
            return None

        self._timestamps[user_id] = now
        self._maybe_cleanup(now)
        return await handler(event, data)

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < _CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        cutoff = now - _STALE_THRESHOLD
        self._timestamps = {
            uid: ts for uid, ts in self._timestamps.items() if ts > cutoff
        }
