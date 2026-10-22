"""
通信系统

客服消息使用 Redis 队列，与工作流调度分开。
工作流节点由 WorkflowExecutor 直接调用 agent.run()。
"""

from communication.queue import CSMessageQueue, QueueWorker

__all__ = [
    "CSMessageQueue",
    "QueueWorker",
]
