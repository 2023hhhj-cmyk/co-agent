"""
Layer 3: 长期记忆 (Knowledge Memory)

长期知识存在 PostgreSQL，向量用 pgvector 检索。
- 向量由阿里云百炼 text-embedding-v4 计算
- 可按 agent_role、memory_type 过滤
- 数据库或嵌入接口不可用时降级为空结果，不影响其他层
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None
_COLUMN_FILTERS = {"agent_id", "agent_role", "memory_type"}


def _vector_literal(vector: List[float]) -> str:
    return "[" + ",".join(format(float(value), ".8f") for value in vector) + "]"


def _load_metadata(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


async def _embed_text(text: str) -> List[float]:
    """调用百炼兼容接口，把一段文本变成向量。"""
    if not settings.DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")

    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)

    url = f"{settings.DASHSCOPE_BASE_URL.rstrip('/')}/embeddings"
    response = await _http_client.post(
        url,
        headers={"Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}"},
        json={
            "model": settings.DASHSCOPE_EMBEDDING_MODEL,
            "input": text,
            "dimensions": settings.DASHSCOPE_EMBEDDING_DIMENSIONS,
        },
    )
    response.raise_for_status()
    vector = response.json()["data"][0]["embedding"]
    expected = settings.DASHSCOPE_EMBEDDING_DIMENSIONS
    if len(vector) != expected:
        raise ValueError(f"嵌入维度不符: 期望 {expected}，实际 {len(vector)}")
    return vector


class KnowledgeMemory:
    """长期记忆 — PostgreSQL + pgvector"""

    def __init__(self, database=None, embedding_cache=None):
        self._database = database
        self._embed_cache = embedding_cache
        self._ready = False
        self._setup_done = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def setup(self) -> None:
        """创建 pgvector 扩展和知识表。失败时这一层保持不可用。"""
        self._setup_done = True
        if not settings.DASHSCOPE_API_KEY:
            logger.warning("DASHSCOPE_API_KEY 未配置，语义检索不可用")
            return
        if self._database is None or not getattr(self._database, "database_url", ""):
            logger.warning("PostgreSQL 未配置，语义检索不可用")
            return

        dimension = int(settings.DASHSCOPE_EMBEDDING_DIMENSIONS)
        try:
            await self._database.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await self._database.execute(
                f"""CREATE TABLE IF NOT EXISTS agent_knowledge (
                    id VARCHAR(64) PRIMARY KEY,
                    agent_id VARCHAR(64) NOT NULL,
                    agent_role VARCHAR(64) NOT NULL,
                    memory_type VARCHAR(64) NOT NULL,
                    content TEXT NOT NULL,
                    embedding vector({dimension}) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{{}}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            await self._database.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_knowledge_role ON agent_knowledge(agent_role)"
            )
            await self._database.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_knowledge_type ON agent_knowledge(memory_type)"
            )
            try:
                await self._database.execute(
                    """CREATE INDEX IF NOT EXISTS idx_agent_knowledge_embedding
                       ON agent_knowledge USING hnsw (embedding vector_cosine_ops)"""
                )
            except Exception as e:
                logger.warning(f"pgvector 索引未创建，检索仍可用: {e}")
            self._ready = True
            logger.info("KnowledgeMemory (PostgreSQL + pgvector) 已就绪")
        except Exception as e:
            self._ready = False
            logger.warning(f"KnowledgeMemory 初始化失败，语义检索不可用: {e}")

    async def _ensure_ready(self) -> bool:
        if not self._setup_done:
            await self.setup()
        return self._ready

    async def _embed(self, text: str) -> List[float]:
        """写入和查询共用同一嵌入接口，相同文本走缓存。"""
        model = settings.DASHSCOPE_EMBEDDING_MODEL
        if self._embed_cache:
            cached = await self._embed_cache.get(text, model)
            if cached:
                return cached

        vector = await _embed_text(text)
        if self._embed_cache:
            await self._embed_cache.set(text, vector, model)
        return vector

    async def add_knowledge(
        self,
        content: str,
        agent_id: str,
        agent_role: str,
        memory_type: str = "knowledge",
        metadata: Optional[Dict[str, Any]] = None,
        knowledge_id: Optional[str] = None,
    ) -> Optional[str]:
        """存储知识。失败返回 None。"""
        if not await self._ensure_ready():
            logger.warning("KnowledgeMemory 不可用，跳过写入")
            return None

        knowledge_id = knowledge_id or str(uuid.uuid4())
        extra = dict(metadata or {})
        extra["timestamp"] = datetime.now().isoformat()

        try:
            embedding = await self._embed(content)
            await self._database.execute(
                """INSERT INTO agent_knowledge
                   (id, agent_id, agent_role, memory_type, content, embedding, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)
                   ON CONFLICT (id) DO UPDATE SET
                       content = EXCLUDED.content,
                       embedding = EXCLUDED.embedding,
                       metadata = EXCLUDED.metadata,
                       memory_type = EXCLUDED.memory_type""",
                knowledge_id,
                agent_id,
                agent_role,
                memory_type,
                content,
                _vector_literal(embedding),
                json.dumps(extra, ensure_ascii=False),
            )
            logger.info(f"知识已存储: {knowledge_id} (type={memory_type})")
            return knowledge_id
        except Exception as e:
            logger.error(f"知识写入失败: {e}")
            return None

    async def search(
        self,
        query: str,
        n_results: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """按余弦距离检索。filter_metadata 支持列等值过滤。"""
        if not await self._ensure_ready():
            logger.warning("KnowledgeMemory 不可用，返回空结果")
            return []

        try:
            query_embedding = await self._embed(query)
            clauses: List[str] = []
            args: List[Any] = [_vector_literal(query_embedding)]
            for key, value in (filter_metadata or {}).items():
                placeholder = f"${len(args) + 1}"
                if key in _COLUMN_FILTERS:
                    clauses.append(f"{key} = {placeholder}")
                    args.append(str(value))
                else:
                    clauses.append(f"metadata @> {placeholder}::jsonb")
                    args.append(json.dumps({key: value}, ensure_ascii=False))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            limit_placeholder = f"${len(args) + 1}"
            args.append(n_results)
            rows = await self._database.fetch(
                f"""SELECT id, agent_id, agent_role, memory_type, content, metadata,
                           1 - (embedding <=> $1::vector) AS score
                    FROM agent_knowledge
                    {where}
                    ORDER BY embedding <=> $1::vector
                    LIMIT {limit_placeholder}""",
                *args,
            )
            items = []
            for row in rows:
                metadata = _load_metadata(row.get("metadata"))
                metadata.update({
                    "agent_id": row["agent_id"],
                    "agent_role": row["agent_role"],
                    "memory_type": row["memory_type"],
                })
                items.append({
                    "id": row["id"],
                    "content": row["content"],
                    "metadata": metadata,
                    "score": round(max(0.0, float(row["score"])), 4),
                })
            return items
        except Exception as e:
            logger.error(f"知识检索失败: {e}")
            return []

    async def forget(self, knowledge_id: str) -> bool:
        """删除指定知识。"""
        if not await self._ensure_ready():
            return False
        try:
            await self._database.execute(
                "DELETE FROM agent_knowledge WHERE id = $1",
                knowledge_id,
            )
            logger.info(f"知识已删除: {knowledge_id}")
            return True
        except Exception as e:
            logger.error(f"知识删除失败: {e}")
            return False

    async def count(self) -> int:
        """知识条目数。"""
        if not await self._ensure_ready():
            return 0
        try:
            row = await self._database.fetchrow("SELECT COUNT(*) AS n FROM agent_knowledge")
            return int(row["n"]) if row else 0
        except Exception:
            return 0
