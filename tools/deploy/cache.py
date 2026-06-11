"""
缓存管理器 - 缓存项目分析结果

核心功能：
- 文件缓存（JSON 格式）
- 缓存键：项目目录的 hash 值
- 缓存过期：24 小时
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 默认缓存目录
DEFAULT_CACHE_DIR = "/tmp/devops_cache"

# 缓存过期时间（秒）
CACHE_EXPIRY = 24 * 60 * 60  # 24 小时


class CacheManager:
    """
    缓存管理器

    使用文件系统缓存项目分析结果。
    """

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, expiry: int = CACHE_EXPIRY):
        """
        初始化缓存管理器

        Args:
            cache_dir: 缓存目录
            expiry: 缓存过期时间（秒）
        """
        self.cache_dir = cache_dir
        self.expiry = expiry

        # 确保缓存目录存在
        os.makedirs(cache_dir, exist_ok=True)

    def get(self, key: str) -> Optional[dict]:
        """
        获取缓存

        Args:
            key: 缓存键（项目目录路径）

        Returns:
            Optional[dict]: 缓存内容，如果不存在或已过期则返回 None
        """
        cache_file = self._get_cache_file(key)

        if not os.path.exists(cache_file):
            return None

        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 检查是否过期
            if time.time() - cache_data.get("timestamp", 0) > self.expiry:
                logger.info(f"缓存已过期: {key}")
                os.remove(cache_file)
                return None

            logger.info(f"命中缓存: {key}")
            return cache_data.get("data")

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"读取缓存失败: {key} - {e}")
            return None

    def set(self, key: str, data: dict):
        """
        设置缓存

        Args:
            key: 缓存键（项目目录路径）
            data: 缓存内容
        """
        cache_file = self._get_cache_file(key)

        cache_data = {
            "timestamp": time.time(),
            "key": key,
            "data": data,
        }

        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)

            logger.info(f"缓存已保存: {key}")

        except IOError as e:
            logger.warning(f"保存缓存失败: {key} - {e}")

    def delete(self, key: str):
        """
        删除缓存

        Args:
            key: 缓存键（项目目录路径）
        """
        cache_file = self._get_cache_file(key)

        if os.path.exists(cache_file):
            try:
                os.remove(cache_file)
                logger.info(f"缓存已删除: {key}")
            except IOError as e:
                logger.warning(f"删除缓存失败: {key} - {e}")

    def clear(self):
        """清空所有缓存"""
        try:
            for file in Path(self.cache_dir).glob("*.json"):
                file.unlink()
            logger.info("缓存已清空")
        except IOError as e:
            logger.warning(f"清空缓存失败: {e}")

    def _get_cache_file(self, key: str) -> str:
        """
        获取缓存文件路径

        Args:
            key: 缓存键

        Returns:
            str: 缓存文件路径
        """
        # 使用 key 的 hash 值作为文件名
        key_hash = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{key_hash}.json")
