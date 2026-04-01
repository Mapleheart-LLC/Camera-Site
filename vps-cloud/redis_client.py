"""
redis_client.py – Shared async Redis client.

Provides a lazily-initialised singleton that is safe to use as a FastAPI
``Depends`` dependency across routers without creating circular imports.
"""

import os

import redis.asyncio as aioredis

# Override via the REDIS_URL environment variable in production / docker-compose.
REDIS_URL: str = os.environ.get("REDIS_URL", "redis://redis:6379")

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return the shared async Redis client, creating it on first call."""
    global _client
    if _client is None:
        _client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Close the shared Redis client and release the connection pool."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
