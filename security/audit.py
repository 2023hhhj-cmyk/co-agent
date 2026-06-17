"""
AuditLogger — 审计日志系统

记录所有关键安全事件到 audit_logs 表（已有基础设施）。

事件类型：
- input_rejected: InputGuard 拒绝的输入
- task_executed: Agent 执行任务
- llm_call: LLM API 调用
- file_access: ClaudeCodeTool 文件操作
- deploy: DevOps 部署操作
- auth_failure: 权限校验失败
- memory_access: 记忆系统操作
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AuditLogger:
    """审计日志记录器"""

    def __init__(self, database=None):
        """
        Args:
            database: Database 实例，不提供则仅输出日志
        """
        self.db = database

    async def log(
        self,
        action: str,
        agent_id: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        记录审计事件

        Args:
            action: 事件类型 (input_rejected/task_executed/llm_call/...)
            agent_id: Agent ID
            details: 事件详情

        Returns:
            audit_id
        """
        audit_id = str(uuid.uuid4())
        details = details or {}

        # 1. 始终输出日志
        detail_str = json.dumps(details, ensure_ascii=False, default=str)[:500]
        logger.info(f"[AUDIT] {action} | agent={agent_id[:8]} | {detail_str}")

        # 2. 写入数据库
        if self.db:
            try:
                await self.db.execute(
                    """INSERT INTO audit_logs (agent_id, action, details, created_at)
                       VALUES ($1, $2, $3, $4)""",
                    agent_id or "system",
                    action,
                    json.dumps(details, ensure_ascii=False, default=str),
                    datetime.now(),
                )
            except Exception as e:
                logger.error(f"审计日志写入失败: {e}")

        return audit_id

    # ========== 便捷方法 ==========

    async def log_input_rejected(
        self,
        content: str,
        source: str,
        reason: str,
        flags: list,
        agent_id: str = "",
    ) -> str:
        """记录输入拒绝事件"""
        return await self.log(
            action="input_rejected",
            agent_id=agent_id,
            details={
                "source": source,
                "reason": reason,
                "flags": flags,
                "content_preview": content[:200],
            },
        )

    async def log_task_executed(
        self,
        agent_id: str,
        task: str,
        result: str = "",
        duration_ms: int = 0,
    ) -> str:
        """记录任务执行事件"""
        return await self.log(
            action="task_executed",
            agent_id=agent_id,
            details={
                "task_preview": task[:200],
                "result_preview": result[:200] if result else "",
                "duration_ms": duration_ms,
            },
        )

    async def log_llm_call(
        self,
        agent_id: str,
        model: str,
        prompt_preview: str,
        result_preview: str = "",
        tokens: int = 0,
    ) -> str:
        """记录 LLM 调用事件"""
        return await self.log(
            action="llm_call",
            agent_id=agent_id,
            details={
                "model": model,
                "prompt_preview": prompt_preview[:200],
                "result_preview": result_preview[:200],
                "tokens": tokens,
            },
        )

    async def log_file_access(
        self,
        agent_id: str,
        operation: str,
        file_path: str,
        success: bool = True,
    ) -> str:
        """记录文件访问事件"""
        return await self.log(
            action="file_access",
            agent_id=agent_id,
            details={
                "operation": operation,
                "file_path": file_path,
                "success": success,
            },
        )

    async def log_deploy(
        self,
        agent_id: str,
        service_name: str,
        action: str,
        result: str = "",
    ) -> str:
        """记录部署操作事件"""
        return await self.log(
            action="deploy",
            agent_id=agent_id,
            details={
                "service_name": service_name,
                "action": action,
                "result": result,
            },
        )

    async def log_auth_failure(
        self,
        agent_id: str,
        action: str,
        reason: str,
    ) -> str:
        """记录权限校验失败事件"""
        return await self.log(
            action="auth_failure",
            agent_id=agent_id,
            details={
                "attempted_action": action,
                "reason": reason,
            },
        )

    async def log_memory_access(
        self,
        agent_id: str,
        operation: str,
        memory_id: str = "",
        query: str = "",
    ) -> str:
        """记录记忆系统操作事件"""
        return await self.log(
            action="memory_access",
            agent_id=agent_id,
            details={
                "operation": operation,
                "memory_id": memory_id,
                "query_preview": query[:200] if query else "",
            },
        )
