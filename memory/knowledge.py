"""
Layer 3: 长期记忆 (Knowledge Memory)

基于 ChromaDB 向量数据库的语义检索系统。
- 使用 sentence_transformers 本地计算 embedding
- 单一 collection + metadata 过滤，支持跨 Agent 知识共享
- ChromaDB 不可用时降级为空结果，不影响其他层

Collection schema:
  collection_name: agent_knowledge
  metadata fields: agent_id, agent_role, memory_type, task_id, project_name, timestamp
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

# 懒加载全局变量
_chroma_client = None
_embedding_fn = None
_collection = None


def _get_embedding_fn():
    """懒加载 sentence_transformers 模型"""
    global _embedding_fn
    if _embedding_fn is None:
        try:
            from sentence_transformers import SentenceTransformer
            model_name = settings.EMBEDDING_MODEL
            logger.info(f"Loading embedding model: {model_name}")
            _embedding_fn = SentenceTransformer(model_name)
            logger.info(f"Embedding model loaded: {model_name}")
        except ImportError:
            logger.error(
                "sentence-transformers 未安装，请运行: pip install sentence-transformers"
            )
            raise
        except Exception as e:
            logger.error(f"加载 embedding 模型失败: {e}")
            raise
    return _embedding_fn


def _get_collection():
    """懒加载 ChromaDB collection"""
    global _chroma_client, _collection
    if _collection is None:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            _chroma_client = chromadb.HttpClient(
                host=settings.CHROMADB_URL.replace("http://", "").split(":")[0],
                port=int(settings.CHROMADB_URL.split(":")[-1]),
                settings=ChromaSettings(anonymized_telemetry=False),
            )

            collection_name = settings.CHROMA_COLLECTION_NAME
            try:
                _collection = _chroma_client.get_collection(name=collection_name)
                logger.info(f"ChromaDB collection '{collection_name}' 已加载")
            except Exception:
                _collection = _chroma_client.create_collection(
                    name=collection_name,
                    metadata={"description": "Agent knowledge memory for semantic search"},
                )
                logger.info(f"ChromaDB collection '{collection_name}' 已创建")
        except Exception as e:
            logger.error(f"ChromaDB 连接失败: {e}")
            raise
    return _collection


class KnowledgeMemory:
    """长期记忆 — ChromaDB 向量语义检索"""

    def __init__(self, embedding_cache=None):
        self._ready = False
        self._embed_cache = embedding_cache
        try:
            self._collection = _get_collection()
            self._embed_fn = _get_embedding_fn()
            self._ready = True
        except Exception as e:
            logger.warning(f"KnowledgeMemory 初始化失败，语义检索不可用: {e}")
            self._collection = None
            self._embed_fn = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ========== 写入 ==========

    async def add_knowledge(
        self,
        content: str,
        agent_id: str,
        agent_role: str,
        memory_type: str = "knowledge",
        metadata: Optional[Dict[str, Any]] = None,
        knowledge_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        存储知识到向量数据库

        Args:
            content: 知识文本内容
            agent_id: Agent ID
            agent_role: Agent 角色
            memory_type: 类型 — "experience" | "faq" | "best_practice" | "project_knowledge"
            metadata: 额外元数据
            knowledge_id: 知识 ID，不提供则自动生成

        Returns:
            knowledge_id，失败返回 None
        """
        if not self._ready:
            logger.warning("KnowledgeMemory 不可用，跳过写入")
            return None

        knowledge_id = knowledge_id or str(uuid.uuid4())

        # 构建 metadata
        doc_metadata = {
            "agent_id": agent_id,
            "agent_role": agent_role,
            "memory_type": memory_type,
            "timestamp": datetime.now().isoformat(),
            **(metadata or {}),
        }

        try:
            # 本地计算 embedding
            embedding = self._embed_fn.encode([content]).tolist()

            self._collection.add(
                ids=[knowledge_id],
                embeddings=embedding,
                documents=[content],
                metadatas=[doc_metadata],
            )
            logger.info(f"知识已存储: {knowledge_id} (type={memory_type})")
            return knowledge_id
        except Exception as e:
            logger.error(f"ChromaDB 写入失败: {e}")
            return None

    # ========== 检索 ==========

    async def search(
        self,
        query: str,
        n_results: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        语义检索相关知识

        Args:
            query: 查询文本
            n_results: 返回结果数
            filter_metadata: ChromaDB where 过滤条件
                例如 {"agent_role": "developer"} 只检索 Developer 的知识
                例如 {"memory_type": "faq"} 只检索 FAQ

        Returns:
            搜索结果列表 [{"id", "content", "metadata", "score"}, ...]
        """
        if not self._ready:
            logger.warning("KnowledgeMemory 不可用，返回空结果")
            return []

        try:
            # 嵌入缓存：相同查询不重复计算
            query_embedding = None
            if self._embed_cache:
                query_embedding = await self._embed_cache.get(query)

            if query_embedding is None:
                query_embedding = self._embed_fn.encode([query]).tolist()
                if self._embed_cache:
                    await self._embed_cache.set(query, query_embedding)

            results = self._collection.query(
                query_embeddings=query_embedding,
                n_results=n_results,
                where=filter_metadata,
                include=["documents", "metadatas", "distances"],
            )

            # 格式化结果
            items = []
            if results and results.get("ids") and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = results["distances"][0][i] if results.get("distances") else 0
                    # 将距离转换为相似度分数（cosine distance → similarity）
                    score = max(0.0, 1.0 - float(distance))

                    item = {
                        "id": doc_id,
                        "content": results["documents"][0][i] if results.get("documents") else "",
                        "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
                        "score": round(score, 4),
                    }
                    items.append(item)

            items.sort(key=lambda x: x["score"], reverse=True)
            return items

        except Exception as e:
            logger.error(f"ChromaDB 检索失败: {e}")
            return []

    # ========== 删除 ==========

    async def forget(self, knowledge_id: str) -> bool:
        """删除指定知识"""
        if not self._ready:
            return False

        try:
            self._collection.delete(ids=[knowledge_id])
            logger.info(f"知识已删除: {knowledge_id}")
            return True
        except Exception as e:
            logger.error(f"ChromaDB 删除失败: {e}")
            return False

    # ========== 统计 ==========

    async def count(self) -> int:
        """获取知识库中的条目数"""
        if not self._ready:
            return 0
        try:
            return self._collection.count()
        except Exception:
            return 0
