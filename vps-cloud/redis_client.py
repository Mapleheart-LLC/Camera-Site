"""
redis_client.py – Shared async Redis client.

Provides a lazily-initialised singleton that is safe to use as a FastAPI
``Depends`` dependency across routers without creating circular imports.

If Redis is not reachable, ``get_redis`` logs a warning and returns ``None``
so that callers can skip Redis-dependent behaviour (e.g. rate-limiting)
without crashing the application.
"""

import logging
import os
from typing import Optional

import redis.asyncio as aioredis

# Override via the REDIS_URL environment variable in production / docker-compose.
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://redis:6379")

_client: Optional[aioredis.Redis] = None

logger = logging.getLogger(__name__)


async def get_redis() -> Optional[aioredis.Redis]:
    """Return the shared async Redis client, or None if Redis is unavailable."""
    global _client
    if _client is None:
        try:
            client = aioredis.from_url(
                REDIS_URL, decode_responses=True, socket_connect_timeout=2
            )
            await client.ping()
            _client = client
        except (aioredis.RedisError, OSError) as exc:
            logger.warning(
                "Redis unavailable (%s); rate-limiting will be skipped.", exc
            )
            return None
    return _client


async def close_redis() -> None:
    """Close the shared Redis client and release the connection pool."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
