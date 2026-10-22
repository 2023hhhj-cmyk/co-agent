"""
AccessControl — Agent 权限控制系统

基于角色的最小权限（RBAC），控制：
1. 消息订阅权限（Agent 只能 watch 角色允许的消息类型）
2. 动作执行权限（Agent 只能执行角色允许的动作）
3. 工具访问权限（Agent 只能使用角色允许的工具）

定义各 Agent 角色的权限边界：
┌──────────────┬──────────────────────────────────┬────────────────────────────┐
│ Role         │ allowed_watch                     │ allowed_tools              │
├──────────────┼──────────────────────────────────┼────────────────────────────┤
│ ceo          │ user_requirement, task_complete,   │ feishu, llm,               │
│              │ report, feishu_message             │ task_decomposition         │
│ developer    │ ceo_task_assignment, create_prd,   │ opencode, llm           │
│              │ architecture_design                │                            │
│ frontend     │ ceo_task_assignment, frontend_task,│ opencode, llm           │
│              │ ui_design, create_prd              │                            │
│ devops       │ review_code, deploy_request,       │ docker, nginx, opencode │
│              │ deploy_fix                         │                            │
│ cs           │ customer_message                   │ wecom, database, faq       │
└──────────────┴──────────────────────────────────┴────────────────────────────┘
"""

import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# 角色权限定义
ROLE_PERMISSIONS: Dict[str, dict] = {
    "ceo": {
        "allowed_watch": {
            "user_requirement", "task_complete", "report",
            "feishu_message", "ceo_action",
        },
        "allowed_tools": {"feishu", "llm", "task_decomposition", "team_coordination"},
        "allowed_actions": {
            "classify_intent", "check_completeness", "decompose_tasks",
            "execute_plan", "report_progress",
        },
    },
    "developer": {
        "allowed_watch": {
            "ceo_task_assignment", "create_prd", "architecture_design",
            "bug_report",
        },
        "allowed_tools": {"opencode", "llm", "code_editor", "git", "testing_framework"},
        "allowed_actions": {
            "design_architecture", "implement_feature", "review_code",
        },
    },
    "frontend_expert": {
        "allowed_watch": {
            "ceo_task_assignment", "frontend_task", "ui_design", "create_prd",
        },
        "allowed_tools": {"opencode", "llm", "playwright", "design_templates"},
        "allowed_actions": {
            "analyze_requirement", "setup_project", "implement_frontend", "review_and_polish",
        },
    },
    "devops": {
        "allowed_watch": {
            "review_code", "deploy_request", "deploy_fix", "ceo_task_assignment",
        },
        "allowed_tools": {"docker", "nginx", "monitoring", "opencode"},
        "allowed_actions": {
            "deploy", "monitor", "fix", "report",
        },
    },
    "customer_service": {
        "allowed_watch": {
            "customer_message",
        },
        "allowed_tools": {"wecom", "database", "product_doc", "faq"},
        "allowed_actions": {
            "classify_intent", "answer_project_progress",
            "answer_project_usage", "answer_feature_request",
            "answer_faq", "escalate_to_human",
        },
    },
}


class PolicyEnforcer:
    """权限策略执行器"""

    def can_watch(self, agent_role: str, message_types: Set[str]) -> bool:
        """
        检查 Agent 是否有权订阅消息类型

        Args:
            agent_role: Agent 角色名
            message_types: 要订阅的消息类型集合

        Returns:
            是否允许
        """
        if agent_role not in ROLE_PERMISSIONS:
            logger.warning(f"未知角色: {agent_role}，拒绝订阅")
            return False

        allowed = ROLE_PERMISSIONS[agent_role].get("allowed_watch", set())
        forbidden = message_types - allowed

        if forbidden:
            logger.warning(
                f"[AccessControl] {agent_role} 试图订阅禁止的消息类型: {forbidden}"
            )
            return False

        return True

    def can_act(self, agent_role: str, action_name: str) -> bool:
        """
        检查 Agent 是否有权执行某个动作

        Args:
            agent_role: Agent 角色名
            action_name: 动作名称

        Returns:
            是否允许
        """
        if agent_role not in ROLE_PERMISSIONS:
            return False

        allowed = ROLE_PERMISSIONS[agent_role].get("allowed_actions", set())
        if action_name not in allowed:
            logger.warning(
                f"[AccessControl] {agent_role} 尝试执行禁止的动作: {action_name}"
            )
            return False

        return True

    def can_use_tool(self, agent_role: str, tool_name: str) -> bool:
        """
        检查 Agent 是否有权使用工具

        Args:
            agent_role: Agent 角色名
            tool_name: 工具名

        Returns:
            是否允许
        """
        if agent_role not in ROLE_PERMISSIONS:
            return False

        allowed = ROLE_PERMISSIONS[agent_role].get("allowed_tools", set())
        return tool_name in allowed

    def validate_message_send(
        self,
        sender_role: str,
        cause_by: str,
        target_roles: Optional[Set[str]] = None,
    ) -> bool:
        """
        验证 Agent 发送消息的合法性

        检查发送者是否有权发出该 cause_by 类型的消息。
        """
        if sender_role not in ROLE_PERMISSIONS:
            return False

        # CEO 可以发送任何类型
        if sender_role == "ceo":
            return True

        # 其他角色只能发送其动作对应的消息类型
        allowed = ROLE_PERMISSIONS[sender_role].get("allowed_watch", set())
        # 也可以发送自身的动作类型（如 developer 发 architecture_design）
        allowed_actions = ROLE_PERMISSIONS[sender_role].get("allowed_actions", set())

        if cause_by in allowed or cause_by in allowed_actions:
            return True

        # error 类型允许（Agent 报告错误）
        if cause_by == "error":
            return True

        logger.warning(
            f"[AccessControl] {sender_role} 试图发送未授权的消息: cause_by={cause_by}"
        )
        return False
