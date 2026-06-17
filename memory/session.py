"""
Layer 2: 中期记忆 (Session Memory)

跨会话历史存储，采用「写时双写 + Redis 纯热缓存」策略：
- 写入：同时写 Redis（7天 TTL）和 PostgreSQL（永久）
- 读取：先查 Redis → miss 则查 PostgreSQL → 回填 Redis
- PostgreSQL 是真相来源（source of truth）

Redis 数据结构：
  agent:{agent_id}:tasks          → List[task_id]    最近任务 ID 列表
  agent:{agent_id}:task:{task_id} → JSON              单个任务详情（7天 TTL）
  agent:{agent_id}:chat:{chat_id} → List[JSON]        对话历史（7天 TTL）
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from memory.importance import ImportanceScorer
from storage.database import Database
from storage.state_manager import StateManager

logger = logging.getLogger(__name__)

# Redis 配置
REDIS_TASK_LIST_KEY = "agent:{agent_id}:tasks"
REDIS_TASK_KEY = "agent:{agent_id}:task:{task_id}"
REDIS_CHAT_KEY = "agent:{agent_id}:chat:{chat_id}"
REDIS_ACCESS_KEY = "agent:{agent_id}:access:{memory_id}"
REDIS_TASK_TTL = 604800   # 7 天
REDIS_CHAT_TTL = 604800   # 7 天
MAX_TASK_LIST = 50        # 每个 Agent 最多保留 50 个任务索引

# Key 白名单：仅允许字母数字、连字符、下划线，防止 Redis 命名空间注入
import re as _re
_KEY_SAFE_PATTERN = _re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_key_part(value: str, name: str) -> None:
    """校验 Redis key 组成部分，拒绝含 : 或特殊字符的输入"""
    if not _KEY_SAFE_PATTERN.match(value):
        raise ValueError(f"[Security] 非法的 Redis key 组成部分: {name}={value}")


class SessionMemory:
    """中期记忆 — Redis 热缓存 + PostgreSQL 永久存储"""

    def __init__(
        self,
        state_manager: StateManager,
        database: Database,
        scorer: Optional[ImportanceScorer] = None,
    ):
        self.redis = state_manager
        self.db = database
        self.scorer = scorer or ImportanceScorer()

    # ========== 写入 ==========

    async def remember_task(
        self,
        agent_id: str,
        agent_role: str,
        task_data: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> str:
        """
        存储任务记忆（双写 Redis + PostgreSQL）

        Args:
            agent_id: Agent 唯一 ID
            agent_role: Agent 角色名
            task_data: 任务数据（包含 content, metadata 等）
            task_id: 任务 ID，不提供则自动生成

        Returns:
            task_id
        """
        task_id = task_id or str(uuid.uuid4())
        _validate_key_part(agent_id, "agent_id")
        _validate_key_part(task_id, "task_id")
        content = task_data.get("content", json.dumps(task_data, ensure_ascii=False))
        metadata = task_data.get("metadata", {})

        # 计算重要性评分
        importance = self.scorer.score(
            content=content,
            agent_role=agent_role,
            memory_type="task",
            created_at=datetime.now().isoformat(),
            access_count=0,
        )
        metadata["importance_score"] = importance

        task_record = {
            "task_id": task_id,
            "agent_id": agent_id,
            "agent_role": agent_role,
            "content": content,
            "memory_type": "task",
            "metadata": metadata,
            "created_at": datetime.now().isoformat(),
            "importance_score": importance,
        }

        # 1. 写入 PostgreSQL（永久存储）
        try:
            await self.db.insert_memory(
                memory_id=task_id,
                agent_id=agent_id,
                agent_role=agent_role,
                content=content,
                memory_type="task",
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"PostgreSQL 存储任务记忆失败: {e}")

        # 2. 写入 Redis（热缓存）
        try:
            task_key = REDIS_TASK_KEY.format(agent_id=agent_id, task_id=task_id)
            await self.redis.set(task_key, task_record, expire=REDIS_TASK_TTL)

            list_key = REDIS_TASK_LIST_KEY.format(agent_id=agent_id)
            await self.redis.lpush(list_key, task_id)
            # 裁剪列表，保持上限
            await self.redis.ltrim(list_key, 0, MAX_TASK_LIST - 1)
        except Exception as e:
            logger.warning(f"Redis 存储任务记忆失败（不影响 PG 数据）: {e}")

        return task_id

    async def remember_chat(
        self,
        agent_id: str,
        agent_role: str,
        chat_id: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """
        存储对话历史（双写 Redis + PostgreSQL）

        Args:
            agent_id: Agent 唯一 ID
            agent_role: Agent 角色名
            chat_id: 会话 ID
            messages: 消息列表 [{"role": "user", "content": "..."}, ...]

        Returns:
            memory_id
        """
        memory_id = str(uuid.uuid4())
        _validate_key_part(agent_id, "agent_id")
        _validate_key_part(chat_id, "chat_id")
        content = json.dumps(messages, ensure_ascii=False)

        # 计算重要性评分
        importance = self.scorer.score(
            content=content,
            agent_role=agent_role,
            memory_type="chat",
            created_at=datetime.now().isoformat(),
            access_count=0,
        )
        metadata = {
            "chat_id": chat_id,
            "message_count": len(messages),
            "importance_score": importance,
        }

        # 1. 写入 PostgreSQL
        try:
            await self.db.insert_memory(
                memory_id=memory_id,
                agent_id=agent_id,
                agent_role=agent_role,
                content=content,
                memory_type="chat",
                metadata=metadata,
            )
        except Exception as e:
            logger.error(f"PostgreSQL 存储对话记忆失败: {e}")

        # 2. 写入 Redis
        try:
            chat_key = REDIS_CHAT_KEY.format(agent_id=agent_id, chat_id=chat_id)
            for msg in messages:
                await self.redis.rpush(chat_key, json.dumps(msg, ensure_ascii=False))
            await self.redis.expire(chat_key, REDIS_CHAT_TTL)
        except Exception as e:
            logger.warning(f"Redis 存储对话记忆失败（不影响 PG 数据）: {e}")

        return memory_id

    # ========== 读取 ==========

    async def recall_tasks(
        self,
        agent_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        检索 Agent 的任务记忆

        流程：Redis List → 逐个 GET → miss 的从 PG 回填
        """
        _validate_key_part(agent_id, "agent_id")
        tasks = []
        missed_ids = []

        try:
            # 1. 从 Redis 获取最近任务 ID 列表
            list_key = REDIS_TASK_LIST_KEY.format(agent_id=agent_id)
            task_ids = await self.redis.lrange(list_key, 0, limit - 1)

            # 2. 逐个获取任务详情
            for task_id in task_ids:
                task_key = REDIS_TASK_KEY.format(agent_id=agent_id, task_id=task_id)
                data = await self.redis.get(task_key)
                if data:
                    tasks.append(data)
                else:
                    missed_ids.append(task_id)
        except Exception as e:
            logger.warning(f"Redis 读取任务列表失败，回退到 PostgreSQL: {e}")
            missed_ids = []  # Redis 错误时直接全量查 PG

        # 3. 对 Redis miss 的 ID，从 PostgreSQL 回填
        if missed_ids or not tasks:
            try:
                pg_memories = await self.db.query_memories(
                    agent_id=agent_id,
                    memory_type="task",
                    limit=limit,
                )
                for mem in pg_memories:
                    pg_metadata = mem.get("metadata", {})
                    # asyncpg 可能将 JSONB 作为字符串返回
                    if isinstance(pg_metadata, str):
                        try:
                            pg_metadata = json.loads(pg_metadata)
                        except (json.JSONDecodeError, TypeError):
                            pg_metadata = {}
                    record = {
                        "task_id": mem["id"],
                        "agent_id": mem["agent_id"],
                        "agent_role": mem.get("agent_role", ""),
                        "content": mem["content"],
                        "memory_type": mem.get("memory_type", "task"),
                        "metadata": pg_metadata,
                        "created_at": mem["created_at"].isoformat() if mem.get("created_at") else "",
                        "importance_score": pg_metadata.get("importance_score", 0.5),
                    }
                    # 回填 Redis
                    try:
                        task_key = REDIS_TASK_KEY.format(agent_id=agent_id, task_id=mem["id"])
                        await self.redis.set(task_key, record, expire=REDIS_TASK_TTL)
                    except Exception:
                        pass
                    tasks.append(record)
            except Exception as e:
                logger.error(f"PostgreSQL 查询任务记忆失败: {e}")

        # 按时间倒序，并更新访问计数
        for task in tasks:
            task["importance_score"] = task.get("metadata", {}).get("importance_score", 0.5)
            # 异步增加访问计数（不影响检索性能）
            try:
                access_key = REDIS_ACCESS_KEY.format(agent_id=agent_id, memory_id=task.get("task_id", ""))
                await self.redis.redis.incr(access_key) if hasattr(self.redis, 'redis') and self.redis.redis else None
            except Exception:
                pass

        tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return tasks[:limit]

    async def recall_chats(
        self,
        agent_id: str,
        chat_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        检索对话历史

        Args:
            agent_id: Agent ID
            chat_id: 指定会话 ID（可选，不指定则返回该 Agent 所有对话）
            limit: 最大返回数
        """
        _validate_key_part(agent_id, "agent_id")
        if chat_id:
            _validate_key_part(chat_id, "chat_id")
        chats = []

        # 如果指定了 chat_id，先尝试 Redis
        if chat_id:
            try:
                chat_key = REDIS_CHAT_KEY.format(agent_id=agent_id, chat_id=chat_id)
                msgs = await self.redis.lrange(chat_key, -limit, -1)
                if msgs:
                    return [json.loads(m) for m in msgs]
            except Exception as e:
                logger.warning(f"Redis 读取对话失败: {e}")

        # Redis miss，查 PostgreSQL
        try:
            pg_memories = await self.db.query_memories(
                agent_id=agent_id,
                memory_type="chat",
                limit=limit,
            )
            for mem in pg_memories:
                try:
                    messages = json.loads(mem["content"])
                    chats.append({
                        "memory_id": mem["id"],
                        "chat_id": mem.get("metadata", {}).get("chat_id", ""),
                        "messages": messages if isinstance(messages, list) else [{"content": mem["content"]}],
                        "created_at": mem["created_at"].isoformat() if mem.get("created_at") else "",
                    })
                except (json.JSONDecodeError, KeyError):
                    chats.append({
                        "memory_id": mem["id"],
                        "chat_id": "",
                        "messages": [{"content": mem["content"]}],
                        "created_at": mem["created_at"].isoformat() if mem.get("created_at") else "",
                    })
        except Exception as e:
            logger.error(f"PostgreSQL 查询对话记忆失败: {e}")

        return chats[:limit]

    # ========== 单条查询 ==========

    async def get_task(self, agent_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单条任务记忆（Redis 优先 → PG 回退）

        Args:
            agent_id: Agent ID
            task_id: 任务记忆 ID

        Returns:
            任务记录 dict，不存在返回 None
        """
        # 先查 Redis
        try:
            task_key = REDIS_TASK_KEY.format(agent_id=agent_id, task_id=task_id)
            data = await self.redis.get(task_key)
            if data:
                return data
        except Exception as e:
            logger.warning(f"Redis 查询任务失败: {e}")

        # 回退 PostgreSQL
        try:
            row = await self.db.fetchrow(
                "SELECT id, agent_id, agent_role, content, memory_type, metadata, created_at FROM memories WHERE id = $1",
                task_id,
            )
            if row:
                record = {
                    "task_id": row["id"],
                    "agent_id": row["agent_id"],
                    "agent_role": row.get("agent_role", ""),
                    "content": row["content"],
                    "memory_type": row.get("memory_type", "task"),
                    "metadata": row.get("metadata", {}),
                    "created_at": row["created_at"].isoformat() if row.get("created_at") else "",
                }
                # 回填 Redis
                try:
                    task_key = REDIS_TASK_KEY.format(agent_id=agent_id, task_id=task_id)
                    await self.redis.set(task_key, record, expire=REDIS_TASK_TTL)
                except Exception:
                    pass
                return record
        except Exception as e:
            logger.error(f"PG 查询任务失败: {e}")

        return None

    # ========== 删除 ==========

    async def forget_task(self, agent_id: str, task_id: str) -> bool:
        """删除任务记忆（Redis + PostgreSQL）"""
        success = True

        # 从 PostgreSQL 删除
        try:
            await self.db.execute("DELETE FROM memories WHERE id = $1", task_id)
        except Exception as e:
            logger.error(f"PostgreSQL 删除记忆失败: {e}")
            success = False

        # 从 Redis 删除
        try:
            task_key = REDIS_TASK_KEY.format(agent_id=agent_id, task_id=task_id)
            await self.redis.delete(task_key)
            # 从列表中移除（lrem 移除值为 task_id 的元素）
            list_key = REDIS_TASK_LIST_KEY.format(agent_id=agent_id)
            await self.redis.delete(list_key)  # 简化处理：重建列表
        except Exception as e:
            logger.warning(f"Redis 删除记忆失败: {e}")

        return success
