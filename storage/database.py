"""
数据库 - PostgreSQL
"""
import logging
import os
from typing import Any, Dict, List, Optional
import asyncpg

logger = logging.getLogger(__name__)


class Database:
    """数据库 — PostgreSQL 操作封装"""

    def __init__(self):
        from config.settings import settings
        self.database_url = settings.DATABASE_URL or os.getenv('DATABASE_URL', '')
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=20)
        logger.info("Database connected to PostgreSQL")

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def execute(self, query: str, *args) -> str:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> List[Dict[str, Any]]:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]

    async def fetchrow(self, query: str, *args) -> Optional[Dict[str, Any]]:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    # ========== 批量操作 ==========

    async def executemany(self, query: str, args_list: List[tuple]) -> None:
        """批量执行相同 SQL 模板，不同参数"""
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            await conn.executemany(query, args_list)

    async def insert_memory(
        self,
        memory_id: str,
        agent_id: str,
        agent_role: str,
        content: str,
        memory_type: str = "task",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """插入一条记忆记录到 memories 表"""
        import json as _json
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO memories (id, agent_id, agent_role, content, memory_type, metadata, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, NOW())
                   ON CONFLICT (id) DO UPDATE SET
                       content = EXCLUDED.content,
                       metadata = EXCLUDED.metadata""",
                memory_id,
                agent_id,
                agent_role,
                content,
                memory_type,
                _json.dumps(metadata or {}),
            )

    async def query_memories(
        self,
        agent_id: Optional[str] = None,
        memory_type: Optional[str] = None,
        agent_role: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询记忆列表，支持按 agent_id / memory_type / agent_role 过滤"""
        if not self.pool:
            await self.connect()

        conditions = []
        params = []
        idx = 1

        if agent_id:
            conditions.append(f"agent_id = ${idx}")
            params.append(agent_id)
            idx += 1
        if memory_type:
            conditions.append(f"memory_type = ${idx}")
            params.append(memory_type)
            idx += 1
        if agent_role:
            conditions.append(f"agent_role = ${idx}")
            params.append(agent_role)
            idx += 1

        where = " AND ".join(conditions) if conditions else "TRUE"
        params.extend([limit, offset])

        query = f"""SELECT id, agent_id, agent_role, content, memory_type, metadata, created_at
                     FROM memories
                     WHERE {where}
                     ORDER BY created_at DESC
                     LIMIT ${idx} OFFSET ${idx + 1}"""

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
