"""
状态管理器 - Redis状态存储
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class StateManager:
    """状态管理器 — Redis 操作封装"""

    def __init__(self):
        from config.settings import settings
        self.redis_url = settings.REDIS_URL or os.getenv('REDIS_URL', '')
        self.redis: Optional[redis.Redis] = None

    async def connect(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        logger.info("StateManager connected to Redis")

    async def disconnect(self):
        if self.redis:
            await self.redis.close()

    # ========== 基础操作 ==========

    async def get(self, key: str) -> Optional[Any]:
        if not self.redis:
            await self.connect()
        data = await self.redis.get(key)
        return json.loads(data) if data else None

    async def set(self, key: str, value: Any, expire: int = 3600):
        if not self.redis:
            await self.connect()
        await self.redis.set(key, json.dumps(value), ex=expire)

    async def delete(self, key: str):
        if not self.redis:
            await self.connect()
        await self.redis.delete(key)

    async def exists(self, key: str) -> bool:
        """检查 key 是否存在"""
        if not self.redis:
            await self.connect()
        return bool(await self.redis.exists(key))

    async def expire(self, key: str, seconds: int) -> bool:
        """设置 key 的过期时间（秒）"""
        if not self.redis:
            await self.connect()
        return bool(await self.redis.expire(key, seconds))

    # ========== List 操作（用于消息队列、任务列表） ==========

    async def lpush(self, key: str, *values: str) -> int:
        """向列表头部插入一个或多个值，返回列表长度"""
        if not self.redis:
            await self.connect()
        return await self.redis.lpush(key, *values)

    async def rpush(self, key: str, *values: str) -> int:
        """向列表尾部插入一个或多个值，返回列表长度"""
        if not self.redis:
            await self.connect()
        return await self.redis.rpush(key, *values)

    async def lrange(self, key: str, start: int, end: int) -> List[str]:
        """获取列表指定范围的元素（0-indexed，-1 表示末尾）"""
        if not self.redis:
            await self.connect()
        return await self.redis.lrange(key, start, end)

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        """裁剪列表，只保留 [start, end] 范围的元素"""
        if not self.redis:
            await self.connect()
        return bool(await self.redis.ltrim(key, start, end))

    async def llen(self, key: str) -> int:
        """获取列表长度"""
        if not self.redis:
            await self.connect()
        return await self.redis.llen(key)

    # ========== 批量操作 ==========

    async def mget(self, keys: List[str]) -> List[Optional[Any]]:
        """批量获取多个 key 的值"""
        if not self.redis:
            await self.connect()
        if not keys:
            return []
        results = await self.redis.mget(keys)
        return [json.loads(r) if r else None for r in results]
