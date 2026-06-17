"""
LLM 响应缓存 + 嵌入缓存

缓存架构 (Redis):
  llm:cache:{hash16}     → JSON {response, model, temp, hit_count, created_at}  TTL 1h
  embed:cache:{hash16}   → JSON [vector_float_list]                             TTL 1h

不缓存：
  - temperature > 0.5（需要多样性）
  - stream=True
  - 错误结果
  - 显式 use_cache=False
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from observability.metrics import llm_cache_hit, llm_cache_miss

logger = logging.getLogger(__name__)

# 缓存 TTL
LLM_CACHE_TTL = 3600         # LLM 响应 1 小时
EMBED_CACHE_TTL = 86400      # 嵌入向量 24 小时（确定性输出，可长缓存）
CACHE_TEMP_THRESHOLD = 0.5   # temperature > 此值不缓存


class ResponseCache:
    """LLM 响应缓存 (Redis)"""

    def __init__(self, state_manager=None):
        self.redis = state_manager

    def _cache_key(self, prompt: str, model: str, temperature: float) -> str:
        """生成缓存 key"""
        content = f"{prompt}|{model}|{temperature:.1f}"
        h = hashlib.sha256(content.encode()).hexdigest()[:32]
        return f"llm:cache:{h}"

    async def get(self, prompt: str, model: str, temperature: float) -> Optional[str]:
        """获取缓存的响应"""
        if not self.redis:
            return None

        key = self._cache_key(prompt, model, temperature)
        try:
            data = await self.redis.get(key)
            if data:
                hit_count = data.get("hit_count", 0) + 1
                # 更新命中计数
                data["hit_count"] = hit_count
                await self.redis.set(key, data, expire=LLM_CACHE_TTL)
                llm_cache_hit.labels(model=model).inc()
                logger.debug(f"LLM cache hit: {key} (hits={hit_count})")
                return data.get("response", "")
        except Exception as e:
            logger.warning(f"缓存读取失败: {e}")

        llm_cache_miss.labels(model=model).inc()
        return None

    async def set(
        self,
        prompt: str,
        model: str,
        temperature: float,
        response: str,
        success: bool = True,
    ) -> bool:
        """写入缓存"""
        if not self.redis or not success:
            return False
        if temperature > CACHE_TEMP_THRESHOLD:
            return False

        key = self._cache_key(prompt, model, temperature)
        try:
            await self.redis.set(
                key,
                {
                    "response": response,
                    "model": model,
                    "temperature": temperature,
                    "hit_count": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                expire=LLM_CACHE_TTL,
            )
            logger.debug(f"LLM cache set: {key}")
            return True
        except Exception as e:
            logger.warning(f"缓存写入失败: {e}")
            return False

    async def invalidate(self, prompt: str = "", model: str = "") -> int:
        """清除缓存：支持按 pattern 匹配删除"""
        if not self.redis:
            return 0
        deleted = 0
        pattern = f"llm:cache:{'*' if not prompt else ''}"
        try:
            cursor = 0
            while True:
                cursor, keys = await self.redis.redis.scan(
                    cursor, match=pattern, count=100
                )
                if keys:
                    for key in keys:
                        await self.redis.redis.delete(key)
                        deleted += 1
                if cursor == 0:
                    break
            logger.info(f"缓存已清除: {deleted} 条 (pattern={pattern})")
        except Exception as e:
            logger.warning(f"缓存清除失败: {e}")
        return deleted


class EmbeddingCache:
    """嵌入向量缓存 (Redis)"""

    def __init__(self, state_manager=None):
        self.redis = state_manager

    def _cache_key(self, text: str, model_name: str) -> str:
        h = hashlib.sha256(f"{text}|{model_name}".encode()).hexdigest()[:32]
        return f"embed:cache:{h}"

    async def get(self, text: str, model_name: str = "all-MiniLM-L6-v2") -> Optional[list]:
        """获取缓存的嵌入向量"""
        if not self.redis:
            return None

        key = self._cache_key(text, model_name)
        try:
            data = await self.redis.get(key)
            if data:
                return data.get("vector", [])
        except Exception as e:
            logger.warning(f"嵌入缓存读取失败: {e}")
        return None

    async def set(self, text: str, vector: list, model_name: str = "all-MiniLM-L6-v2") -> bool:
        """缓存嵌入向量"""
        if not self.redis:
            return False

        key = self._cache_key(text, model_name)
        try:
            await self.redis.set(key, {"vector": vector, "model": model_name}, expire=EMBED_CACHE_TTL)
            return True
        except Exception as e:
            logger.warning(f"嵌入缓存写入失败: {e}")
            return False
