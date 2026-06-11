"""
通信系统

提供消息传递基础设施：
- MessageBus: 消息总线（发布/订阅）
- Message: 消息模型（从 models 导入）
"""

from communication.message_bus import MessageBus

__all__ = [
    "MessageBus",
]
