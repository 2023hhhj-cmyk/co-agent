"""
状态管理器 - Redis状态存储
"""
import os
import json
from typing import Any, Dict, Optional
import redis.asyncio as redis

class StateManager:
    """状态管理器"""

    def __init__(self):
        self.redis_url = os.getenv('REDIS_URL', 'redis://:muzhu_redis_2024@localhost:6379/0')
        self.redis = None

    async def connect(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        print("StateManager connected to Redis")

    async def disconnect(self):
        if self.redis:
            await self.redis.close()

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
