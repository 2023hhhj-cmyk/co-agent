"""
数据模型层

提供核心数据结构定义：
- Message: 统一消息格式
- AgentRole: Agent 角色定义
- WorkflowDef: 工作流图定义
- DeployRecord: 部署记录
"""

from models.message import Message, MessageRole, MessageQueue
from models.agent import AgentRole, ActionDef, ReactMode
from models.task import Task, TaskStatus, ProjectPlan
from models.workflow import (
    WorkflowDef,
    GraphDef,
    NodeDef,
    EdgeDef,
    NodeType,
)
from models.deploy import (
    DeployRecord,
    ServiceStatus,
    ServiceHealth,
    HealthStatus,
    PortAllocation,
)

__all__ = [
    # 消息
    "Message",
    "MessageRole",
    "MessageQueue",
    # Agent
    "AgentRole",
    "ActionDef",
    "ReactMode",
    # 任务
    "Task",
    "TaskStatus",
    "ProjectPlan",
    # 工作流
    "WorkflowDef",
    "GraphDef",
    "NodeDef",
    "EdgeDef",
    "NodeType",
    # 部署
    "DeployRecord",
    "ServiceStatus",
    "ServiceHealth",
    "HealthStatus",
    "PortAllocation",
]
