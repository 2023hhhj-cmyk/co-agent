"""
客户会话模型 - 客服 Agent 使用

设计：
- CustomerSession: 客户会话，包含对话历史和关联项目
- 会话存储在 Redis（快速读写）+ PostgreSQL（持久化）
- 每个客户独立上下文，滑动窗口控制历史长度
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class CustomerSession(BaseModel):
    """客户会话"""

    customer_id: str                              # 企业微信外部联系人 ID
    customer_name: str = ""                       # 客户姓名
    project_ids: list[str] = Field(default_factory=list)  # 关联的项目 ID 列表
    history: list[dict[str, str]] = Field(default_factory=list)  # 对话历史
    metadata: dict[str, Any] = Field(default_factory=dict)  # 扩展信息
    created_at: datetime = Field(default_factory=datetime.now)
    last_active: datetime = Field(default_factory=datetime.now)

    class Config:
        # 对话历史最大轮数（不作为 Pydantic 字段）
        max_history_rounds = 10

    def add_round(self, customer_msg: str, agent_reply: str) -> None:
        """添加一轮对话（滑动窗口）"""
        self.history.append({
            "customer": customer_msg,
            "agent": agent_reply,
            "time": datetime.now().isoformat(),
        })
        # 超出窗口时移除最早的
        if len(self.history) > self.MAX_HISTORY_ROUNDS:
            self.history = self.history[-self.MAX_HISTORY_ROUNDS:]
        self.last_active = datetime.now()

    def get_history_text(self) -> str:
        """获取对话历史的文本形式（用于 LLM 上下文）"""
        if not self.history:
            return ""
        lines = []
        for h in self.history:
            lines.append(f"客户：{h['customer']}")
            lines.append(f"客服：{h['agent']}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """序列化为字典（用于 Redis 存储）"""
        data = self.model_dump()
        data["created_at"] = self.created_at.isoformat()
        data["last_active"] = self.last_active.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CustomerSession":
        """从字典反序列化"""
        if isinstance(data.get("created_at"), str):
            data["created_at"] = datetime.fromisoformat(data["created_at"])
        if isinstance(data.get("last_active"), str):
            data["last_active"] = datetime.fromisoformat(data["last_active"])
        return cls(**data)


class QueuedMessage(BaseModel):
    """队列中的消息"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    customer_id: str          # 外部联系人 ID
    customer_name: str = ""   # 客户姓名（从企业微信获取）
    text: str                 # 消息内容
    wecom_msg_id: str = ""    # 企业微信消息 ID
    metadata: dict[str, Any] = Field(default_factory=dict)  # 扩展信息（如 response_url）
    enqueued_at: datetime = Field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        data = self.model_dump()
        data["enqueued_at"] = self.enqueued_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "QueuedMessage":
        if isinstance(data.get("enqueued_at"), str):
            data["enqueued_at"] = datetime.fromisoformat(data["enqueued_at"])
        return cls(**data)
