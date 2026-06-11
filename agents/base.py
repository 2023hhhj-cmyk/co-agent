"""
Agent 基类 - 融合 MetaGPT 角色机制

核心设计（借鉴 MetaGPT）：
- Think-Act-Observe 三阶段循环
- watch 订阅机制：只处理关心的消息
- cause_by 标记：消息来源追踪
- 反应模式：REACT / BY_ORDER / PLAN_AND_ACT
"""

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message, MessageQueue
from tools.llm.client import LLMClient
from tools.llm.prompt import PromptManager

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Agent 基类

    融合 MetaGPT 的 Role 设计：
    - role: 角色定义（name, profile, goal, constraints）
    - watch: 订阅的消息类型
    - react_mode: 反应模式
    - Think-Act-Observe: 核心执行循环
    """

    def __init__(
        self,
        role: AgentRole,
        llm_client: Optional[LLMClient] = None,
        message_bus: Any = None,
    ):
        # 基础属性
        self.agent_id = str(uuid.uuid4())
        self.role = role
        self.llm = llm_client
        self.message_bus = message_bus
        self.prompt_manager = PromptManager()

        # 状态
        self.state = "idle"  # idle, busy, error
        self.created_at = datetime.now()

        # 消息和记忆
        self.message_queue: MessageQueue = MessageQueue()
        self.memory: List[Message] = []  # 工作记忆
        self.working_memory: List[Message] = []  # 当前任务记忆

        # 动作状态
        self.todo: ActionDef | None = None
        self.state_index: int = 0  # BY_ORDER 模式下的状态索引

        # 注册到消息总线
        if message_bus and role.watch:
            message_bus.register_agent(self.agent_id, role.watch)

    # ========== 属性 ==========

    @property
    def name(self) -> str:
        return self.role.name

    @property
    def is_idle(self) -> bool:
        """是否空闲"""
        return (
            self.state == "idle"
            and self.todo is None
            and self.message_queue.is_empty()
        )

    # ========== MetaGPT 三阶段循环 ==========

    async def _observe(self) -> List[Message]:
        """
        观察阶段：从消息队列读取消息

        借鉴 MetaGPT：
        - 只处理 watch 匹配的消息
        - 过滤掉自己发送的消息
        """
        # 先从本地队列获取消息（run() 方法推入的）
        local_messages = await self.message_queue.pop_all()

        if self.message_bus:
            # 再从消息总线获取消息（其他 Agent 发布的）
            bus_messages = await self.message_bus.get_messages(self.agent_id)
            messages = local_messages + bus_messages
        else:
            messages = local_messages

        # 过滤：只保留 watch 匹配的消息
        if self.role.watch:
            filtered = []
            for msg in messages:
                if msg.cause_by in self.role.watch or not msg.cause_by:
                    filtered.append(msg)
            messages = filtered

        # 添加到记忆
        for msg in messages:
            self.memory.append(msg)

        return messages

    @abstractmethod
    async def _think(self, messages: List[Message]) -> Optional[ActionDef]:
        """
        思考阶段：LLM 决策下一步动作

        子类实现具体的决策逻辑

        Args:
            messages: 观察到的消息

        Returns:
            要执行的动作，或 None（无需动作）
        """
        pass

    @abstractmethod
    async def _act(self, action: ActionDef, context: Optional[Dict[str, Any]] = None) -> Message:
        """
        执行阶段：执行动作，生成消息

        子类实现具体的执行逻辑

        Args:
            action: 要执行的动作
            context: 上下文信息

        Returns:
            执行结果消息
        """
        pass

    async def _react(self, messages: List[Message]) -> Optional[Message]:
        """
        REACT 模式：Think-Act 循环

        借鉴 MetaGPT 的反应模式：
        - 循环执行 Think-Act
        - 直到没有新动作或达到最大循环次数
        """
        result = None
        for _ in range(self.role.max_react_loop):
            action = await self._think(messages)
            if action is None:
                break
            result = await self._act(action)
            # 将结果加入消息，供下一轮 Think 使用
            messages.append(result)
        return result

    async def _by_order(self, messages: List[Message]) -> Optional[Message]:
        """
        BY_ORDER 模式：按顺序执行动作列表

        借鉴 MetaGPT：
        - 按 actions 列表顺序执行
        - 每次执行一个动作
        """
        if not self.role.actions:
            return None

        if self.state_index >= len(self.role.actions):
            self.state_index = 0  # 重置
            return None

        action = self.role.actions[self.state_index]
        self.state_index += 1
        return await self._act(action, {"messages": messages})

    # ========== 主入口 ==========

    async def run(self, message: Optional[Message] = None) -> Optional[Message]:
        """
        主执行入口

        流程：
        1. 如果有输入消息，加入队列
        2. 执行 _observe() 获取消息
        3. 如果有消息，根据 react_mode 执行
        4. 返回结果
        """
        # 注入输入消息
        if message:
            await self.message_queue.push(message)

        # 观察
        messages = await self._observe()
        if not messages:
            return None

        # 更新状态
        self.state = "busy"
        try:
            # 根据反应模式执行
            if self.role.react_mode == ReactMode.REACT:
                result = await self._react(messages)
            elif self.role.react_mode == ReactMode.BY_ORDER:
                result = await self._by_order(messages)
            elif self.role.react_mode == ReactMode.PLAN_AND_ACT:
                result = await self._plan_and_act(messages)
            else:
                result = await self._react(messages)

            # 发布结果
            if result:
                await self.publish_message(result)

            return result

        except Exception as e:
            logger.error(f"Agent {self.name} error: {e}")
            self.state = "error"
            raise
        finally:
            self.state = "idle"
            self.todo = None

    async def _plan_and_act(self, messages: List[Message]) -> Optional[Message]:
        """
        PLAN_AND_ACT 模式：先规划再执行

        借鉴 MetaGPT 的 Planner 模式
        """
        # TODO: 实现规划逻辑
        # 暂时回退到 REACT 模式
        return await self._react(messages)

    # ========== 消息发布 ==========

    async def publish_message(self, message: Message) -> None:
        """
        发布消息到消息总线

        借鉴 MetaGPT 的 publish_message
        """
        # 设置发送者
        message.sent_from = self.agent_id

        # 发布到消息总线
        if self.message_bus:
            await self.message_bus.publish(message)

        # 同时保存到记忆
        self.memory.append(message)

    def put_message(self, message: Message) -> None:
        """
        直接放入本地队列（不经过消息总线）

        借鉴 MetaGPT 的 put_message
        """
        asyncio.create_task(self.message_queue.push(message))

    # ========== 辅助方法 ==========

    def get_memories(self, k: int = 10) -> List[Message]:
        """获取最近 k 条记忆"""
        return self.memory[-k:]

    def get_history_text(self, k: int = 10) -> str:
        """获取最近 k 条记忆的文本形式"""
        memories = self.get_memories(k)
        return "\n".join([f"[{m.sent_from}]: {m.content}" for m in memories])

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role.to_dict(),
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "memory_count": len(self.memory),
        }
