"""
消息总线 - 融合 MetaGPT 订阅机制

核心设计：
- 支持 cause_by 订阅：Agent 只接收关心的消息类型
- 本地队列 + Redis 持久化：实时通信 + 消息持久化
- 定向投递 + 广播：支持点对点和广播

P1 #9 修复：可选 Redis 持久化层
  - 进程重启时消息不丢
  - 跨实例可观测消息流
  - 注意：消息传递仍是 in-memory 实时通路，Redis 仅做审计/恢复
"""

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from models.message import Message, MessageQueue

logger = logging.getLogger(__name__)


class MessageBus:
    """
    消息总线

    借鉴 MetaGPT 的订阅机制：
    - Agent 通过 watch 集合订阅感兴趣的 cause_by 类型
    - 消息总线根据 cause_by 过滤并投递消息

    P1 #9：可选 state_manager 注入做 Redis 持久化
    """

    # Redis 历史 key
    HISTORY_KEY = "msg_bus:history"
    HISTORY_TTL = 86400 * 7  # 7 天

    def __init__(self, state_manager: Optional[Any] = None):
        # Agent 消息队列：agent_id -> MessageQueue
        self._queues: Dict[str, MessageQueue] = {}
        # Agent 订阅：agent_id -> set of cause_by
        self._subscriptions: Dict[str, Set[str]] = defaultdict(set)
        # cause_by 到 Agent 的映射：cause_by -> set of agent_ids
        self._cause_by_agents: Dict[str, Set[str]] = defaultdict(set)
        # 所有订阅了广播的 Agent
        self._broadcast_subscribers: Set[str] = set()
        # 消息历史（可选，用于调试）
        self._history: List[Message] = []
        self._max_history = 1000
        # P1 #9：可选 Redis 持久化
        self._state_manager = state_manager
        if state_manager:
            logger.info("MessageBus 已启用 Redis 持久化")
        else:
            logger.info("MessageBus 仅内存模式（消息重启丢失）")

    def register_agent(self, agent_id: str, watch: Optional[Set[str]] = None) -> None:
        """
        注册 Agent 到消息总线

        Args:
            agent_id: Agent 唯一标识
            watch: 订阅的 cause_by 集合
        """
        if agent_id not in self._queues:
            self._queues[agent_id] = MessageQueue()

        if watch:
            self._subscriptions[agent_id] = watch
            for cause_by in watch:
                self._cause_by_agents[cause_by].add(agent_id)
        else:
            # 没有指定 watch，订阅所有广播
            self._broadcast_subscribers.add(agent_id)

        logger.info(f"Agent {agent_id} registered with watch: {watch}")

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent"""
        # 清理订阅
        for cause_by in self._subscriptions.get(agent_id, set()):
            self._cause_by_agents[cause_by].discard(agent_id)
        self._subscriptions.pop(agent_id, None)
        self._broadcast_subscribers.discard(agent_id)
        self._queues.pop(agent_id, None)
        logger.info(f"Agent {agent_id} unregistered")

    def update_watch(self, agent_id: str, watch: set[str]) -> None:
        """更新 Agent 的订阅"""
        # 清理旧订阅
        old_watch = self._subscriptions.get(agent_id, set())
        for cause_by in old_watch:
            self._cause_by_agents[cause_by].discard(agent_id)

        # 设置新订阅
        self._subscriptions[agent_id] = watch
        for cause_by in watch:
            self._cause_by_agents[cause_by].add(agent_id)

        logger.info(f"Agent {agent_id} watch updated to: {watch}")

    async def publish(self, message: Message) -> None:
        """
        发布消息

        路由逻辑：
        1. 如果 send_to 指定了具体 Agent，直接投递
        2. 如果是广播（"*"），投递给订阅了该 cause_by 的 Agent
        3. 记录消息历史（内存 + 可选 Redis）
        """
        # 记录历史
        self._history.append(message)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # P1 #9：异步持久化到 Redis（不阻塞主流程）
        if self._state_manager:
            try:
                # 用 LPUSH + LTRIM 维持最近 N 条
                msg_json = json.dumps(message.to_dict(), ensure_ascii=False, default=str)
                # 启动一个 fire-and-forget 任务持久化
                asyncio.create_task(self._persist_message(msg_json))
            except Exception as e:
                logger.warning(f"消息持久化失败（非阻塞）: {e}")

        if message.is_broadcast():
            # 广播：投递给订阅了该 cause_by 的 Agent
            target_agents = self._cause_by_agents.get(message.cause_by, set())
            # 也投递给订阅了所有广播的 Agent
            target_agents = target_agents | self._broadcast_subscribers
        else:
            # 定向投递
            target_agents = message.send_to

        # 投递消息
        for agent_id in target_agents:
            if agent_id in self._queues and agent_id != message.sent_from:
                await self._queues[agent_id].push(message)
                logger.debug(f"Message delivered to {agent_id}: cause_by={message.cause_by}")

    async def _persist_message(self, msg_json: str) -> None:
        """异步持久化单条消息到 Redis（fire-and-forget）"""
        try:
            await self._state_manager.lpush(self.HISTORY_KEY, msg_json)
            # 限制 Redis 历史长度（保留最近 1000 条）
            await self._state_manager.ltrim(self.HISTORY_KEY, 0, 999)
            await self._state_manager.expire(self.HISTORY_KEY, self.HISTORY_TTL)
        except Exception as e:
            logger.warning(f"持久化消息到 Redis 失败: {e}")

    async def get_persisted_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """P1 #9：从 Redis 恢复历史消息（用于重启后审计/重放）

        Returns:
            消息字典列表（按时间倒序）
        """
        if not self._state_manager:
            return []
        try:
            raw = await self._state_manager.lrange(self.HISTORY_KEY, 0, limit - 1)
            return [json.loads(m) for m in raw if m]
        except Exception as e:
            logger.error(f"恢复消息历史失败: {e}")
            return []

    async def get_messages(self, agent_id: str) -> List[Message]:
        """获取 Agent 的所有待处理消息"""
        if agent_id not in self._queues:
            return []
        return await self._queues[agent_id].pop_all()

    async def get_message(self, agent_id: str, timeout: Optional[float] = None) -> Optional[Message]:
        """获取 Agent 的一条消息（支持超时）"""
        if agent_id not in self._queues:
            return None
        try:
            return await self._queues[agent_id].pop(timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def has_messages(self, agent_id: str) -> bool:
        """检查 Agent 是否有待处理消息"""
        if agent_id not in self._queues:
            return False
        return not self._queues[agent_id].is_empty()

    def get_subscribers(self, cause_by: str) -> Set[str]:
        """获取订阅了指定 cause_by 的所有 Agent"""
        return self._cause_by_agents.get(cause_by, set()).copy()

    def get_history(self, limit: int = 100) -> List[Message]:
        """获取消息历史"""
        return self._history[-limit:]

    def clear_history(self) -> None:
        """清空消息历史"""
        self._history.clear()
