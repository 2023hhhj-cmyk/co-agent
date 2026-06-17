"""
MemoryGraph — 记忆关联图谱

基于 Redis Sets 的轻量级图结构，建立记忆之间的关联边。

边类型：
- same_project (0.9): 同一批次归档的记忆
- same_tags (0.7): 共享标签的记忆（来自 Consolidator）

数据结构：
  Redis:
    memory:edges:{memory_id}     → Set[edge_json]   出边集合
    memory:tag:{tag}             → Set[memory_id]    标签倒排索引

  PostgreSQL:
    memory_edges (source_id, target_id, edge_type, weight, created_at)
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from storage.database import Database
from storage.state_manager import StateManager

logger = logging.getLogger(__name__)

# Redis key 前缀
REDIS_EDGES_KEY = "memory:edges:{memory_id}"
REDIS_TAG_INDEX = "memory:tag:{tag}"

# 边权重
EDGE_WEIGHTS = {
    "same_project": 0.9,
    "same_tags": 0.7,
}


class MemoryGraph:
    """记忆关联图谱 — Redis 热索引 + PostgreSQL 持久化"""

    def __init__(self, state_manager: StateManager, database: Database):
        self.redis = state_manager
        self.db = database

    # ========== 边操作 ==========

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        weight: Optional[float] = None,
    ) -> bool:
        """
        添加一条有向边 source → target

        Args:
            source_id: 源记忆 ID
            target_id: 目标记忆 ID
            edge_type: 边类型 (same_project | same_tags)
            weight: 权重，不提供则使用默认值
        """
        if source_id == target_id:
            return False

        weight = weight or EDGE_WEIGHTS.get(edge_type, 0.5)
        edge = json.dumps({
            "source": source_id,
            "target": target_id,
            "type": edge_type,
            "weight": weight,
            "created_at": datetime.now().isoformat(),
        })

        # 1. Redis 热索引
        try:
            key = REDIS_EDGES_KEY.format(memory_id=source_id)
            await self.redis.redis.sadd(key, edge)
            # 也添加反向边（方便双向遍历）
            rev_key = REDIS_EDGES_KEY.format(memory_id=target_id)
            rev_edge = json.dumps({
                "source": target_id,
                "target": source_id,
                "type": edge_type,
                "weight": weight,
                "created_at": datetime.now().isoformat(),
            })
            await self.redis.redis.sadd(rev_key, rev_edge)
        except Exception as e:
            logger.warning(f"Redis 添加边失败: {e}")

        # 2. PostgreSQL 持久化
        try:
            await self.db.execute(
                """INSERT INTO memory_edges (source_id, target_id, edge_type, weight)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (source_id, target_id, edge_type) DO NOTHING""",
                source_id, target_id, edge_type, weight,
            )
        except Exception as e:
            logger.warning(f"PG 添加边失败: {e}")

        return True

    # ========== 批量建边 ==========

    async def create_project_edges(self, memory_ids: List[str]) -> int:
        """
        为一组记忆创建 same_project 边（全连接）

        同批次归档的记忆互相连线。
        """
        count = 0
        for i, src in enumerate(memory_ids):
            for tgt in memory_ids[i + 1:]:
                if await self.add_edge(src, tgt, "same_project"):
                    count += 1
        if count:
            logger.info(f"项目边创建: {len(memory_ids)} 节点 → {count} 条边")
        return count

    async def create_tag_edges(self, memory_id: str, tags: List[str]) -> int:
        """
        为记忆创建 same_tags 边（与共享标签的其他记忆连线）

        流程：
        1. 将 memory_id 加入每个标签的倒排索引
        2. 查询每个标签下已有的其他记忆
        3. 与它们建立双向边
        """
        count = 0
        connected: Set[str] = set()

        for tag in tags:
            tag = tag.strip().lower()
            if not tag:
                continue

            try:
                tag_key = REDIS_TAG_INDEX.format(tag=tag)
                # 获取该标签下已有的记忆
                existing = await self.redis.redis.smembers(tag_key)
                # 将当前记忆加入标签索引
                await self.redis.redis.sadd(tag_key, memory_id)

                for other_id in existing:
                    other_id = other_id.decode() if isinstance(other_id, bytes) else other_id
                    if other_id != memory_id and other_id not in connected:
                        if await self.add_edge(memory_id, other_id, "same_tags"):
                            connected.add(other_id)
                            count += 1
            except Exception as e:
                logger.warning(f"标签边创建失败 tag={tag}: {e}")

        if count:
            logger.info(f"标签边创建: memory={memory_id[:8]} tags={tags} → {count} 条边")
        return count

    # ========== 图遍历 ==========

    async def expand(
        self,
        seed_ids: List[str],
        depth: int = 1,
        min_weight: float = 0.5,
    ) -> Dict[str, float]:
        """
        BFS 扩展：从种子记忆出发，沿边找到关联记忆

        Args:
            seed_ids: 种子记忆 ID 列表
            depth: BFS 深度（默认 1-hop）
            min_weight: 边权重阈值，低于此值的边不扩展

        Returns:
            {memory_id: expanded_score} — 扩展到的记忆及其扩展分数
        """
        visited: Set[str] = set(seed_ids)
        results: Dict[str, float] = {}
        frontier: Set[str] = set(seed_ids)

        for d in range(depth):
            next_frontier: Set[str] = set()
            decay = 1.0 / (d + 1)  # 越远权重越低

            for node_id in frontier:
                try:
                    key = REDIS_EDGES_KEY.format(memory_id=node_id)
                    edges_raw = await self.redis.redis.smembers(key)
                except Exception:
                    # Redis miss → 尝试从 PG 加载
                    edges_raw = await self._load_edges_from_pg(node_id)

                for edge_raw in edges_raw:
                    try:
                        edge_str = edge_raw.decode() if isinstance(edge_raw, bytes) else edge_raw
                        edge = json.loads(edge_str)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    weight = edge.get("weight", 0.5)
                    target = edge.get("target", "")

                    if weight < min_weight or not target:
                        continue
                    if target in visited:
                        continue

                    visited.add(target)
                    next_frontier.add(target)

                    # 扩展分数 = 边权重 × 深度衰减
                    expanded_score = weight * decay
                    if target not in results or expanded_score > results[target]:
                        results[target] = round(expanded_score, 4)

            frontier = next_frontier
            if not frontier:
                break

        return results

    # ========== PG 回退 ==========

    async def _load_edges_from_pg(self, memory_id: str) -> List[str]:
        """从 PostgreSQL 加载边并回填 Redis"""
        try:
            rows = await self.db.fetch(
                """SELECT source_id, target_id, edge_type, weight
                   FROM memory_edges
                   WHERE source_id = $1""",
                memory_id,
            )
            edges = []
            for row in rows:
                edge = json.dumps({
                    "source": row["source_id"],
                    "target": row["target_id"],
                    "type": row["edge_type"],
                    "weight": row["weight"],
                    "created_at": datetime.now().isoformat(),
                })
                edges.append(edge)
                # 回填 Redis
                try:
                    key = REDIS_EDGES_KEY.format(memory_id=memory_id)
                    await self.redis.redis.sadd(key, edge)
                except Exception:
                    pass
            return edges
        except Exception as e:
            logger.error(f"PG 边加载失败: {e}")
            return []

    # ========== 删除 ==========

    async def remove_node(self, memory_id: str) -> bool:
        """删除一个节点的所有边"""
        try:
            key = REDIS_EDGES_KEY.format(memory_id=memory_id)
            await self.redis.delete(key)
            await self.db.execute(
                "DELETE FROM memory_edges WHERE source_id = $1 OR target_id = $1",
                memory_id,
            )
            return True
        except Exception as e:
            logger.error(f"删除节点失败: {e}")
            return False

    # ========== 统计 ==========

    async def edge_count(self, memory_id: str) -> int:
        """获取某记忆的出边数"""
        try:
            key = REDIS_EDGES_KEY.format(memory_id=memory_id)
            return await self.redis.redis.scard(key)
        except Exception:
            rows = await self.db.fetch(
                "SELECT COUNT(*) as cnt FROM memory_edges WHERE source_id = $1",
                memory_id,
            )
            return rows[0]["cnt"] if rows else 0
