import json
import time
from typing import Any, Dict, Optional

from .config import settings

try:
    import redis  # type: ignore
except Exception:
    redis = None


class _MemoryCache:
    def __init__(self):
        self._store: Dict[str, Any] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at is not None and time.time() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ex: Optional[int] = None) -> None:
        expires_at = (time.time() + ex) if ex else None
        self._store[key] = (expires_at, value)


class CacheClient:
    def __init__(self):
        self._memory = _MemoryCache()
        self._redis = None

        if settings.use_redis and redis is not None:
            try:
                self._redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
                self._redis.ping()
            except Exception:
                self._redis = None

    def get_json(self, key: str) -> Optional[Any]:
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                if raw is None:
                    return None
                return json.loads(raw)
            except Exception:
                pass
        return self._memory.get(key)

    def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        ttl = ttl if ttl is not None else settings.cache_ttl_sec
        if self._redis is not None:
            try:
                self._redis.set(key, json.dumps(value), ex=ttl)
                return
            except Exception:
                pass
        self._memory.set(key, value, ex=ttl)


cache_client = CacheClient()
