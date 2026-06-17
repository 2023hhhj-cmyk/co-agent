"""
MemoryManager — 三层记忆系统统一入口

职责：
- recall(query, strategy): 融合三层结果，按重要性加权排序 + 图扩展
- remember(content, type): 按类型路由到对应层
- archive_working_memory(): 短期记忆 → 中期记忆归档 + 自动提炼 + 建图边
- forget(memory_id): 从所有层删除
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from memory.consolidator import MemoryConsolidator
from memory.graph import MemoryGraph
from memory.importance import ImportanceScorer
from memory.knowledge import KnowledgeMemory
from memory.session import SessionMemory
from memory.working import WorkingMemory

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """统一的记忆项"""
    memory_id: str
    agent_id: str
    agent_role: str
    content: str
    memory_type: str        # "working" | "task" | "chat" | "experience" | "faq" | "knowledge"
    layer: int              # 1 | 2 | 3
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    score: float = 0.0          # 检索相关度（Layer 3 语义搜索专用）
    importance_score: float = 0.0  # 重要性评分（所有层通用）


class MemoryManager:
    """三层记忆系统管理器（含重要性评分 + 自动提炼）"""

    def __init__(
        self,
        session_memory: SessionMemory,
        knowledge_memory: Optional[KnowledgeMemory] = None,
        scorer: Optional[ImportanceScorer] = None,
        consolidator: Optional[MemoryConsolidator] = None,
        graph: Optional[MemoryGraph] = None,
    ):
        self.session = session_memory
        self.knowledge = knowledge_memory
        self.scorer = scorer or ImportanceScorer()
        self.consolidator = consolidator
        self.graph = graph

        # 短期记忆按 agent_id 管理（每个 Agent 一个 WorkingMemory）
        self._working: Dict[str, WorkingMemory] = {}

    def set_llm_client(self, llm_client: Any) -> None:
        """设置 LLM 客户端（用于自动提炼）"""
        if not self.consolidator:
            self.consolidator = MemoryConsolidator(llm_client=llm_client)
            logger.info("MemoryConsolidator 已启用（LLM 提炼）")
        elif not self.consolidator.is_ready:
            self.consolidator.llm = llm_client
            logger.info("MemoryConsolidator LLM 客户端已注入")

    # ========== Working Memory 管理 ==========

    def get_working(self, agent_id: str) -> WorkingMemory:
        """获取或创建 Agent 的工作记忆"""
        if agent_id not in self._working:
            self._working[agent_id] = WorkingMemory()
        return self._working[agent_id]

    def clear_working(self, agent_id: str) -> None:
        """清空 Agent 的工作记忆"""
        if agent_id in self._working:
            self._working[agent_id].clear()

    # ========== 统一写入 ==========

    async def remember(
        self,
        content: str,
        agent_id: str,
        agent_role: str,
        memory_type: str = "task",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        存储记忆，根据类型自动路由到对应层

        - "working" → Layer 1 (WorkingMemory)
        - "task" / "chat" → Layer 2 (SessionMemory)
        - "experience" / "faq" / "best_practice" / "project_knowledge" → Layer 3 (KnowledgeMemory)
        """
        memory_id = str(uuid.uuid4())

        if memory_type == "working":
            # Layer 1: 通过 add_message 间接写入
            logger.debug(f"Working memory 需通过 add_message 写入: {agent_id}")
            return None

        elif memory_type in ("task", "chat"):
            # Layer 2: SessionMemory
            if memory_type == "task":
                await self.session.remember_task(
                    agent_id=agent_id,
                    agent_role=agent_role,
                    task_data={
                        "content": content,
                        "metadata": metadata or {},
                    },
                    task_id=memory_id,
                )
            else:
                messages = metadata.get("messages", [{"role": "user", "content": content}]) if metadata else [{"role": "user", "content": content}]
                chat_id = metadata.get("chat_id", "default") if metadata else "default"
                await self.session.remember_chat(
                    agent_id=agent_id,
                    agent_role=agent_role,
                    chat_id=chat_id,
                    messages=messages,
                )
            return memory_id

        elif memory_type in ("experience", "faq", "best_practice", "project_knowledge", "knowledge"):
            # Layer 3: KnowledgeMemory
            if self.knowledge and self.knowledge.is_ready:
                return await self.knowledge.add_knowledge(
                    content=content,
                    agent_id=agent_id,
                    agent_role=agent_role,
                    memory_type=memory_type,
                    metadata=metadata,
                    knowledge_id=memory_id,
                )
            else:
                logger.warning("KnowledgeMemory 不可用，知识未存储")
                return None

        else:
            logger.warning(f"未知的记忆类型: {memory_type}，默认存储到 Layer 2")
            await self.session.remember_task(
                agent_id=agent_id,
                agent_role=agent_role,
                task_data={
                    "content": content,
                    "metadata": {"original_type": memory_type, **(metadata or {})},
                },
                task_id=memory_id,
            )
            return memory_id

    # ========== 归档（含自动提炼）==========

    async def archive_working_memory(
        self,
        agent_id: str,
        agent_role: str,
        working_memory: WorkingMemory,
    ) -> Optional[str]:
        """
        将短期记忆归档到中期记忆（任务完成时调用）

        流程：
        1. 快速归档原始数据（不阻塞 Agent）
        2. 异步启动 LLM 提炼（fire-and-forget）
        3. 提炼完成后更新记忆（替换原始内容为结构化摘要）
        """
        messages_dict = working_memory.to_dict_list()
        if not messages_dict:
            return None

        content = working_memory.get_text(k=len(working_memory))
        memory_id = str(uuid.uuid4())
        metadata = {
            "message_count": len(messages_dict),
            "messages": messages_dict,
            "archived_at": datetime.now().isoformat(),
            "consolidated": False,
        }

        # 计算重要性评分
        importance = self.scorer.score(
            content=content,
            agent_role=agent_role,
            memory_type="task",
            created_at=datetime.now().isoformat(),
        )
        metadata["importance_score"] = importance

        await self.session.remember_task(
            agent_id=agent_id,
            agent_role=agent_role,
            task_data={
                "content": content,
                "metadata": metadata,
            },
            task_id=memory_id,
        )

        logger.info(f"Working memory 已归档: agent={agent_role}, messages={len(messages_dict)}, importance={importance}")

        # 异步提炼（不阻塞）
        if self.consolidator and self.consolidator.is_ready:
            asyncio.create_task(
                self._consolidate_and_update(
                    memory_id=memory_id,
                    agent_id=agent_id,
                    agent_role=agent_role,
                    messages=messages_dict,
                )
            )

        return memory_id

    async def _consolidate_and_update(
        self,
        memory_id: str,
        agent_id: str,
        agent_role: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        """
        后台提炼并更新记忆（由 archive_working_memory 异步调用）

        1. 调用 LLM 提炼为结构化摘要
        2. 用摘要替换原始内容
        3. 关键决策和技术要点存入 KnowledgeMemory
        """
        try:
            result = await self.consolidator.consolidate(
                messages=messages,
                metadata={"agent_role": agent_role, "task_name": "auto_archive"},
            )

            if not result:
                logger.debug(f"提炼结果为空，跳过更新: {memory_id}")
                return

            # 构建提炼后的内容（摘要 + 结构化信息）
            parts = [f"## 摘要\n{result['summary']}"]

            if result.get("key_decisions"):
                parts.append(f"## 关键决策\n" + "\n".join(f"- {d}" for d in result["key_decisions"]))
            if result.get("technical_notes"):
                parts.append(f"## 技术要点\n" + "\n".join(f"- {n}" for n in result["technical_notes"]))
            if result.get("tags"):
                parts.append(f"## 标签\n{', '.join(result['tags'])}")
            parts.append(f"## 结果\n{result.get('outcome', 'unknown')}")

            consolidated_content = "\n\n".join(parts)

            # 重新计算评分（提炼后的内容质量更高）
            importance = self.scorer.score(
                content=consolidated_content,
                agent_role=agent_role,
                memory_type="task",
                created_at=datetime.now().isoformat(),
            )

            # 更新 SessionMemory 中的内容
            new_metadata = {
                "consolidated": True,
                "consolidated_at": result.get("consolidated_at", datetime.now().isoformat()),
                "original_message_count": len(messages),
                "outcome": result.get("outcome", "unknown"),
                "tags": result.get("tags", []),
                "importance_score": importance,
            }

            await self.session.remember_task(
                agent_id=agent_id,
                agent_role=agent_role,
                task_data={
                    "content": consolidated_content,
                    "metadata": new_metadata,
                },
                task_id=memory_id,  # 覆盖原记录
            )

            # 关键决策和技术要点存入 KnowledgeMemory
            if self.knowledge and self.knowledge.is_ready:
                for decision in result.get("key_decisions", []):
                    await self.knowledge.add_knowledge(
                        content=f"[决策] {decision}",
                        agent_id=agent_id,
                        agent_role=agent_role,
                        memory_type="decision",
                        metadata={"source_memory_id": memory_id},
                    )
                for note in result.get("technical_notes", []):
                    await self.knowledge.add_knowledge(
                        content=f"[技术要点] {note}",
                        agent_id=agent_id,
                        agent_role=agent_role,
                        memory_type="experience",
                        metadata={"source_memory_id": memory_id},
                    )

            # 创建标签关联边（P2: Memory Graph）
            tags = result.get("tags", [])
            if self.graph and tags:
                try:
                    edge_count = await self.graph.create_tag_edges(memory_id, tags)
                    if edge_count:
                        logger.info(f"记忆关联图: {memory_id[:8]} → {edge_count} 条标签边 tags={tags}")
                except Exception as e:
                    logger.warning(f"创建标签边失败: {e}")

            logger.info(
                f"记忆提炼完成: {memory_id}, "
                f"decisions={len(result.get('key_decisions', []))}, "
                f"notes={len(result.get('technical_notes', []))}, "
                f"importance={importance}"
            )

        except Exception as e:
            logger.error(f"后台提炼失败: {memory_id}, error={e}")

    # ========== 统一检索 ==========

    async def recall(
        self,
        query: str,
        agent_id: str,
        agent_role: str = "",
        strategy: str = "default",
        top_k: int = 10,
    ) -> List[MemoryItem]:
        """
        融合三层记忆检索

        Args:
            query: 查询文本
            agent_id: Agent ID
            agent_role: Agent 角色名
            strategy: "default" | "recent" | "semantic" | "comprehensive" | "important" | "graph"
            top_k: 每层最大返回数

        Returns:
            去重后的 MemoryItem 列表
        """
        results: List[MemoryItem] = []

        # Layer 1: 短期记忆（总是全量返回）
        wm = self.get_working(agent_id)
        for msg in wm.get_all():
            results.append(MemoryItem(
                memory_id=str(uuid.uuid4()),
                agent_id=agent_id,
                agent_role=agent_role,
                content=getattr(msg, 'content', str(msg)),
                memory_type="working",
                layer=1,
                metadata={"sent_from": getattr(msg, 'sent_from', '')},
                created_at=datetime.now().isoformat(),
            ))

        if strategy in ("default", "comprehensive", "important", "graph"):
            # Layer 2: 关键词匹配（或全部用于 important/graph 策略）
            session_tasks = await self.session.recall_tasks(agent_id, limit=top_k)
            keywords = self._extract_keywords(query)
            for task in session_tasks:
                if strategy in ("important", "graph") or self._keyword_match(task.get("content", ""), keywords):
                    results.append(MemoryItem(
                        memory_id=task.get("task_id", ""),
                        agent_id=agent_id,
                        agent_role=task.get("agent_role", agent_role),
                        content=task.get("content", ""),
                        memory_type=task.get("memory_type", "task"),
                        layer=2,
                        metadata=task.get("metadata", {}),
                        created_at=task.get("created_at", ""),
                        importance_score=task.get("importance_score", task.get("metadata", {}).get("importance_score", 0.5)),
                    ))

        elif strategy == "recent":
            # Layer 2: 最近 N 条
            session_tasks = await self.session.recall_tasks(agent_id, limit=top_k)
            for task in session_tasks:
                results.append(MemoryItem(
                    memory_id=task.get("task_id", ""),
                    agent_id=agent_id,
                    agent_role=task.get("agent_role", agent_role),
                    content=task.get("content", ""),
                    memory_type=task.get("memory_type", "task"),
                    layer=2,
                    metadata=task.get("metadata", {}),
                    created_at=task.get("created_at", ""),
                    importance_score=task.get("importance_score", task.get("metadata", {}).get("importance_score", 0.5)),
                ))

        if strategy in ("default", "semantic", "comprehensive"):
            # Layer 3: 语义检索
            if self.knowledge and self.knowledge.is_ready:
                k_results = await self.knowledge.search(query, n_results=top_k)
                for kr in k_results:
                    results.append(MemoryItem(
                        memory_id=kr["id"],
                        agent_id=kr.get("metadata", {}).get("agent_id", agent_id),
                        agent_role=kr.get("metadata", {}).get("agent_role", agent_role),
                        content=kr["content"],
                        memory_type=kr.get("metadata", {}).get("memory_type", "knowledge"),
                        layer=3,
                        metadata=kr.get("metadata", {}),
                        score=kr.get("score", 0.0),
                    ))

        # 去重（以 content 前 100 字符的 hash 为 key）
        results = self._deduplicate(results)

        # 图扩展（P2: Memory Graph）
        if strategy == "graph" and self.graph:
            seed_ids = [r.memory_id for r in results if r.memory_id and r.layer > 1]
            if seed_ids:
                try:
                    expanded = await self.graph.expand(seed_ids, depth=1)
                    for mem_id, graph_score in expanded.items():
                        # 检查是否已在结果中
                        if any(r.memory_id == mem_id for r in results):
                            continue
                        # 从 SessionMemory 获取实际记忆内容
                        task = await self.session.get_task(agent_id, mem_id)
                        if task:
                            pg_metadata = task.get("metadata", {})
                            results.append(MemoryItem(
                                memory_id=mem_id,
                                agent_id=task.get("agent_id", agent_id),
                                agent_role=task.get("agent_role", agent_role),
                                content=task.get("content", ""),
                                memory_type=task.get("memory_type", "task"),
                                layer=4,  # Layer 4 = graph expansion
                                metadata=pg_metadata,
                                created_at=task.get("created_at", ""),
                                score=graph_score,
                                importance_score=pg_metadata.get("importance_score", 0.5),
                            ))
                    if expanded:
                        logger.debug(f"Graph expanded: {len(seed_ids)} seeds → {len(expanded)} new")
                except Exception as e:
                    logger.warning(f"图扩展失败: {e}")

        # 排序（按策略混合 importance + relevance）
        if strategy == "important":
            # 纯按重要性降序
            results.sort(key=lambda x: x.importance_score, reverse=True)
        elif strategy == "semantic":
            # 30% importance + 70% semantic similarity
            results.sort(key=lambda x: 0.3 * x.importance_score + 0.7 * x.score, reverse=True)
        elif strategy == "default":
            # 50% importance + 50% recency (newer = higher)
            now = datetime.now().isoformat()
            results.sort(
                key=lambda x: 0.5 * x.importance_score + 0.5 * (1 if x.created_at else 0),
                reverse=True,
            )
        elif strategy == "comprehensive":
            # 60% importance + 40% recency
            results.sort(key=lambda x: 0.6 * x.importance_score + 0.4 * x.score, reverse=True)
        elif strategy == "graph":
            # 40% importance + 60% graph expansion score
            results.sort(key=lambda x: 0.4 * x.importance_score + 0.6 * x.score, reverse=True)
        else:
            # recent: pure time
            results.sort(key=lambda x: x.created_at, reverse=True)

        return results[:top_k * 3]

    # ========== 删除 ==========

    async def forget(self, memory_id: str, agent_id: str = "") -> bool:
        """从所有层删除指定记忆"""
        success = True

        # Layer 2
        try:
            await self.session.forget_task(agent_id, memory_id)
        except Exception as e:
            logger.warning(f"Layer 2 删除失败: {e}")
            success = False

        # Layer 3
        if self.knowledge and self.knowledge.is_ready:
            try:
                await self.knowledge.forget(memory_id)
            except Exception as e:
                logger.warning(f"Layer 3 删除失败: {e}")
                success = False

        return success

    # ========== 工具方法 ==========

    def _extract_keywords(self, query: str) -> List[str]:
        """
        关键词提取（支持中文）

        1. 先按标点/空格分词
        2. 对中文文本（CJK）额外生成字符级 bigram/trigram
           "支付宝支付功能" → ["支付宝", "支付", "付功", "支付宝支", ...]
        """
        import re
        words = re.split(r'[\s，。！？、；：""''（）\(\)\[\]【】,\.!\?;:]+', query)
        keywords = [w.lower() for w in words if len(w) >= 2]

        # CJK 字符级 n-gram（弥补中文无空格分词的问题）
        cjk_pattern = re.compile(r'[一-鿿㐀-䶿]+')
        for match in cjk_pattern.finditer(query):
            segment = match.group()
            if len(segment) >= 2:
                # bigram: "支付宝" → ["支付", "付宝"]
                for i in range(len(segment) - 1):
                    keywords.append(segment[i:i+2].lower())
                # trigram: "支付宝支付" → ["支付宝", "付宝支", ...]
                for i in range(len(segment) - 2):
                    keywords.append(segment[i:i+3].lower())

        # 去重，保持顺序
        seen = set()
        unique = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique

    def _keyword_match(self, content: str, keywords: List[str]) -> bool:
        """检查 content 是否包含任一关键词"""
        if not keywords:
            return True
        content_lower = content.lower()
        return any(kw in content_lower for kw in keywords)

    def _deduplicate(self, items: List[MemoryItem]) -> List[MemoryItem]:
        """按 content 前 100 字符 hash 去重，保留首次出现"""
        seen = set()
        unique = []
        for item in items:
            h = hashlib.md5(item.content[:100].encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                unique.append(item)
        return unique

    # ========== 记忆淘汰 ==========

    async def cleanup_old_raw_logs(self, days: int = 7) -> int:
        """
        清理超过 N 天的原始日志（raw_log 类型）

        raw_log 是自动提炼前的原始消息流，提炼后已无保留价值。
        定时调用此方法回收存储空间。

        Returns:
            删除的记录数
        """
        try:
            # 从 PostgreSQL 删除
            result = await self.session.db.execute(
                """DELETE FROM memories
                   WHERE memory_type = 'raw_log'
                     AND created_at < NOW() - INTERVAL '1 day' * $1""",
                days,
            )
            # asyncpg execute 返回 "DELETE N" 字符串
            count = 0
            if result:
                try:
                    count = int(result.split()[-1])
                except (ValueError, IndexError):
                    pass

            if count:
                logger.info(f"记忆淘汰: 清理了 {count} 条 raw_log (>{days}天)")
            return count
        except Exception as e:
            logger.error(f"记忆淘汰失败: {e}")
            return 0

    # ========== 上下文组装 ==========

    async def get_context(
        self,
        query: str,
        agent_id: str,
        agent_role: str = "",
        strategy: str = "default",
        max_tokens_estimate: int = 2000,
    ) -> str:
        """
        组装完整上下文文本（用于注入 LLM prompt）

        Args:
            query: 当前查询
            agent_id: Agent ID
            agent_role: Agent 角色
            strategy: 检索策略
            max_tokens_estimate: 最大估计 token 数

        Returns:
            格式化的上下文字符串
        """
        items = await self.recall(query, agent_id, agent_role, strategy=strategy)

        if not items:
            return "（无相关记忆）"

        sections = []
        current_tokens = 0

        for item in items:
            snippet = f"[{item.memory_type.upper()}][{item.layer}] {item.content[:300]}"
            # 粗略估计 token 数（中文约 1.5 字符/token，英文约 4 字符/token）
            estimated = len(snippet) // 2
            if current_tokens + estimated > max_tokens_estimate:
                break
            sections.append(snippet)
            current_tokens += estimated

        return "## 相关记忆\n" + "\n---\n".join(sections)
