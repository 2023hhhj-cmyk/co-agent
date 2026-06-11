"""
消息模型 - 融合 MetaGPT 的 cause_by 标记和 ChatDev 的路由机制

核心设计：
- Message: 统一消息格式，支持 cause_by 来源标记和 send_to 路由
- MessageRole: 消息角色枚举
- MessageQueue: 基于 asyncio.Queue 的异步消息队列
"""

import asyncio
import uuid
from enum import Enum
from typing import Any, Optional, Union
from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Message(BaseModel):
    """
    统一消息格式

    借鉴 MetaGPT:
    - cause_by: 标记消息来源的 Action/Node，用于订阅过滤
    - sent_from: 发送者标识
    - send_to: 接收者集合，支持定向和广播

    借鉴 ChatDev:
    - metadata: 扩展字段，支持任意上下文传递
    """
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    content: str = ""
    role: MessageRole = MessageRole.ASSISTANT
    cause_by: str = ""  # 来源 Action/Node 标记（MetaGPT 核心机制）
    sent_from: str = ""  # 发送者 ID
    send_to: set[str] = Field(default_factory=lambda: {"*"})  # "*" 表示广播
    instruct_content: Optional[dict] = None  # 结构化数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_broadcast(self) -> bool:
        """是否为广播消息"""
        return "*" in self.send_to

    def is_for(self, agent_id: str) -> bool:
        """检查消息是否发送给指定 Agent"""
        return self.is_broadcast() or agent_id in self.send_to

    def to_dict(self) -> dict:
        """序列化为字典"""
        data = self.model_dump()
        data["send_to"] = list(data["send_to"])  # set -> list for JSON
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        """从字典反序列化"""
        if "send_to" in data and isinstance(data["send_to"], list):
            data["send_to"] = set(data["send_to"])
        return cls(**data)


class MessageQueue:
    """
    异步消息队列

    基于 asyncio.Queue，用于 Agent 内部消息缓存
    """

    def __init__(self, maxsize: int = 0):
        self._queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=maxsize)

    async def push(self, message: Message) -> None:
        """推入消息"""
        await self._queue.put(message)

    async def pop(self, timeout: Optional[float] = None) -> Message:
        """弹出消息，支持超时"""
        if timeout:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        return await self._queue.get()

    async def pop_all(self) -> list[Message]:
        """弹出所有消息"""
        messages = []
        while not self._queue.empty():
            try:
                messages.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    def is_empty(self) -> bool:
        """队列是否为空"""
        return self._queue.empty()

    def size(self) -> int:
        """队列大小"""
        return self._queue.qsize()
