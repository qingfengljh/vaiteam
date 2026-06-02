"""
Redis 连接池

用于消息队列和缓存。连接参数从环境变量读取。
"""

import logging
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=20,
        )
        logger.info(f"Redis connected: {settings.REDIS_URL}")
    return _pool


async def close():
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None
        logger.info("Redis connection closed")
