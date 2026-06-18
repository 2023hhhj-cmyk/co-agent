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

P2 #21 修复：审计失败不应静默
  - 写 DB 失败时记录 Prom 指标 + 内存 ring buffer
  - ring buffer 保留最近 100 条失败事件，供后续补偿写入
"""

import json
import logging
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)


class AuditLogger:
    """审计日志记录器"""

    # P2 #21：失败事件 ring buffer（最大 100 条）
    _FAILED_EVENTS_MAX = 100

    def __init__(self, database=None):
        """
        Args:
            database: Database 实例，不提供则仅输出日志
        """
        self.db = database
        # P2 #21：失败事件兜底队列
        self._failed_events: Deque[Dict[str, Any]] = deque(maxlen=self._FAILED_EVENTS_MAX)
        # P2 #21：Prom 指标
        self._init_metrics()

    def _init_metrics(self):
        """P2 #21：初始化审计指标"""
        try:
            from prometheus_client import Counter
            self._metric_write_failures = Counter(
                "audit_write_failures_total",
                "Number of audit log write failures",
                ["action"],
            )
            self._metric_write_success = Counter(
                "audit_writes_total",
                "Number of audit log write successes",
                ["action"],
            )
        except Exception:
            # 无 Prom 环境时降级（用 None 占位）
            self._metric_write_failures = None
            self._metric_write_success = None

    def get_failed_events(self) -> list:
        """P2 #21：获取失败的审计事件（供补偿写入）"""
        return list(self._failed_events)

    async def log(
        self,
        action: str,
        agent_id: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        记录审计事件

        P2 #21：DB 写入失败不再静默
          - 写入 ring buffer 供后续补偿
          - Prom 指标记录失败次数
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
                if self._metric_write_success:
                    self._metric_write_success.labels(action=action).inc()
            except Exception as e:
                # P2 #21：不再静默
                logger.error(f"[Audit] 写入失败 (action={action}): {e}", exc_info=True)
                # 入 ring buffer
                self._failed_events.append({
                    "audit_id": audit_id,
                    "action": action,
                    "agent_id": agent_id,
                    "details": details,
                    "error": str(e),
                    "failed_at": datetime.now().isoformat(),
                })
                if self._metric_write_failures:
                    self._metric_write_failures.labels(action=action).inc()

        return audit_id

    async def retry_failed_events(self) -> int:
        """P2 #21：补偿写入失败事件

        Returns:
            成功补偿写入的数量
        """
        if not self.db or not self._failed_events:
            return 0

        success_count = 0
        remaining = deque()

        for event in self._failed_events:
            try:
                await self.db.execute(
                    """INSERT INTO audit_logs (agent_id, action, details, created_at)
                       VALUES ($1, $2, $3, $4)""",
                    event.get("agent_id") or "system",
                    event["action"],
                    json.dumps(event.get("details", {}), ensure_ascii=False, default=str),
                    datetime.fromisoformat(event["failed_at"]),
                )
                success_count += 1
            except Exception as e:
                logger.warning(f"补偿写入仍失败: {e}")
                remaining.append(event)

        # 更新队列（移除已成功的）
        self._failed_events = remaining
        logger.info(f"[Audit] 补偿写入完成: 成功 {success_count} 条，剩余 {len(remaining)} 条")
        return success_count

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
