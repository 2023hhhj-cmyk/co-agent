"""
LLM 路由器 - 混合策略选择

根据任务类型选择合适的 LLM：
- DeepSeek（主）：简单代码、CRUD、日常任务
- Claude（辅）：复杂推理、代码审查、架构设计
"""

import logging
from enum import Enum

from tools.llm.client import ClaudeClient, DeepSeekClient, LLMClient

logger = logging.getLogger(__name__)


class TaskType(str, Enum):
    """任务类型"""
    SIMPLE_CODE = "simple_code"  # 简单代码生成
    COMPLEX_CODE = "complex_code"  # 复杂代码/重构
    CODE_REVIEW = "code_review"  # 代码审查
    ARCHITECTURE = "architecture"  # 架构设计
    PRD = "prd"  # PRD/需求分析
    DESIGN = "design"  # 设计相关
    DOCUMENT = "document"  # 文档撰写
    GENERAL = "general"  # 通用任务


# 任务类型到 LLM 的映射
TASK_LLM_MAP: dict[TaskType, str] = {
    TaskType.SIMPLE_CODE: "deepseek",
    TaskType.COMPLEX_CODE: "claude",
    TaskType.CODE_REVIEW: "claude",
    TaskType.ARCHITECTURE: "claude",
    TaskType.PRD: "deepseek",
    TaskType.DESIGN: "deepseek",
    TaskType.DOCUMENT: "deepseek",
    TaskType.GENERAL: "deepseek",
}

# Agent 角色到默认任务类型的映射
AGENT_TASK_MAP: dict[str, TaskType] = {
    "ceo": TaskType.GENERAL,
    "product_manager": TaskType.PRD,
    "developer": TaskType.COMPLEX_CODE,
    "ui_designer": TaskType.DESIGN,
    "devops": TaskType.SIMPLE_CODE,
    "finance": TaskType.GENERAL,
}


class LLMRouter:
    """
    LLM 路由器

    根据任务类型或 Agent 角色选择合适的 LLM 客户端
    """

    def __init__(self):
        self._clients: dict[str, LLMClient] = {}

    def _get_or_create_client(self, provider: str) -> LLMClient:
        """获取或创建 LLM 客户端"""
        if provider not in self._clients:
            if provider == "deepseek":
                self._clients[provider] = DeepSeekClient()
            elif provider == "claude":
                self._clients[provider] = ClaudeClient()
            else:
                raise ValueError(f"Unknown LLM provider: {provider}")
        return self._clients[provider]

    def get_client_by_task(self, task_type: TaskType) -> LLMClient:
        """根据任务类型获取 LLM 客户端"""
        provider = TASK_LLM_MAP.get(task_type, "deepseek")
        logger.info(f"Task type {task_type.value} -> provider {provider}")
        return self._get_or_create_client(provider)

    def get_client_by_agent(self, agent_role: str) -> LLMClient:
        """根据 Agent 角色获取默认 LLM 客户端"""
        task_type = AGENT_TASK_MAP.get(agent_role, TaskType.GENERAL)
        return self.get_client_by_task(task_type)

    def get_client(self, provider: str) -> LLMClient:
        """直接根据 provider 名称获取客户端"""
        return self._get_or_create_client(provider)

    async def close_all(self):
        """关闭所有客户端"""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
