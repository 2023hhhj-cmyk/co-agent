"""
Agents模块 - 5个核心Agent定义

所有 Agent 融合 MetaGPT 角色机制：
- Think-Act-Observe 三阶段循环
- watch 订阅机制
- LLM 驱动决策
"""

from agents.base import BaseAgent
from agents.ceo.agent import CEOAgent, CEO_ROLE
from agents.customer_service.agent import CustomerServiceAgent, CS_ROLE
from agents.developer.agent import DeveloperAgent, DEVELOPER_ROLE
from agents.frontend_expert.agent import FrontendExpertAgent, FRONTEND_EXPERT_ROLE
from agents.devops.agent import DevOpsAgent, DEVOPS_ROLE

__all__ = [
    "BaseAgent",
    "CEOAgent",
    "CustomerServiceAgent",
    "DeveloperAgent",
    "FrontendExpertAgent",
    "DevOpsAgent",
    # 角色定义
    "CEO_ROLE",
    "CS_ROLE",
    "DEVELOPER_ROLE",
    "FRONTEND_EXPERT_ROLE",
    "DEVOPS_ROLE",
]
