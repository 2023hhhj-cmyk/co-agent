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

    async def dedup_check(
        self,
        key: str,
        ttl: int = 86400,
    ) -> bool:
        """P1 #15：原子去重检查（防重放）

        使用 Redis SET key value NX EX ttl 原子操作：
          - key 不存在 → 设置成功，返回 True（首次出现）
          - key 已存在 → 设置失败，返回 False（重复）

        Args:
            key: 去重 key（通常是 message_id）
            ttl: key 过期时间（秒），默认 24 小时

        Returns:
            True = 首次出现（应处理）
            False = 重复（应跳过）

        Note: 比内存 set 可靠：
          - 跨进程/跨实例生效
          - 进程重启后仍生效
          - Redis 单线程保证原子性，无需加锁
        """
        if not self.redis:
            await self.connect()
        # SET ... NX EX 是原子操作
        result = await self.redis.set(f"dedup:{key}", "1", nx=True, ex=ttl)
        return bool(result)

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
