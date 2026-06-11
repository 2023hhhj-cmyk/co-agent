"""
数据库 - PostgreSQL
"""
import os
from typing import Any, Dict, List, Optional
import asyncpg

class Database:
    """数据库"""

    def __init__(self):
        self.database_url = os.getenv('DATABASE_URL', 'postgresql://muzhu:muzhu_pg_2024@localhost:5432/muzhu_agent')
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.database_url)
        print("Database connected to PostgreSQL")

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
