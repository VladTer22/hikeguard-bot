import time

import aiohttp
import structlog

logger = structlog.get_logger()

_CAS_API_URL = "https://api.cas.chat/check"
_CAS_TIMEOUT = 5
_CACHE_TTL = 3600  # 1 hour
_MAX_CACHE_SIZE = 10_000


class CASChecker:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[int, tuple[bool, float]] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_CAS_TIMEOUT),
            )
        return self._session

    async def is_banned(self, user_id: int) -> bool:
        cached = self._cache.get(user_id)
        if cached:
            result, ts = cached
            if time.monotonic() - ts < _CACHE_TTL:
                return result

        try:
            session = await self._get_session()
            async with session.get(_CAS_API_URL, params={"user_id": user_id}) as resp:
                data = await resp.json()
                result = data.get("ok", False)
                self._put_cache(user_id, result)
                if result:
                    logger.info("cas_ban_found", user_id=user_id)
                return result
        except (aiohttp.ClientError, TimeoutError):
            logger.warning("cas_check_failed", user_id=user_id)
            return False

    def _put_cache(self, user_id: int, result: bool) -> None:
        if len(self._cache) >= _MAX_CACHE_SIZE:
            # Evict oldest quarter
            by_age = sorted(self._cache.items(), key=lambda kv: kv[1][1])
            for uid, _ in by_age[: _MAX_CACHE_SIZE // 4]:
                del self._cache[uid]
        self._cache[user_id] = (result, time.monotonic())

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
