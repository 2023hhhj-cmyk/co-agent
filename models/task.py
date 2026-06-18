"""
任务数据模型 - CEO Agent 的任务拆解和追踪

核心设计：
- Task: 单个子任务，包含分配、依赖、状态
- ProjectPlan: 完整项目计划，管理任务集合
- TaskStatus: 任务生命周期状态
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务状态"""
    PENDING = "pending"            # 待确认（拆解完成，等用户确认）
    CONFIRMED = "confirmed"        # 已确认，待执行
    RUNNING = "running"            # 执行中
    COMPLETED = "completed"        # 已完成
    FAILED = "failed"              # 失败
    WAITING_DEP = "waiting_dep"    # 等待依赖完成


class Task(BaseModel):
    """单个子任务"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str
    description: str
    assigned_to: str               # agent role name (如 "product_manager")
    dependencies: list[str] = Field(default_factory=list)  # 依赖的 task id
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 2              # 1=高 2=中 3=低
    result: Optional[str] = None   # 执行结果摘要
    error: Optional[str] = None    # 错误信息
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    def mark_running(self):
        self.status = TaskStatus.RUNNING

    def mark_completed(self, result: str = ""):
        self.status = TaskStatus.COMPLETED
        self.result = result
        self.completed_at = datetime.now()

    def mark_failed(self, error: str = ""):
        self.status = TaskStatus.FAILED
        self.error = error
        self.completed_at = datetime.now()

    def to_summary(self) -> str:
        """生成简短的状态摘要"""
        status_icon = {
            TaskStatus.PENDING: "⬜",
            TaskStatus.CONFIRMED: "📋",
            TaskStatus.RUNNING: "🔄",
            TaskStatus.COMPLETED: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.WAITING_DEP: "⏳",
        }
        icon = status_icon.get(self.status, "❓")
        return f"{icon} [{self.id}] {self.title} → {self.assigned_to}"


class ProjectPlan(BaseModel):
    """项目计划"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    requirement: str               # 原始需求文本
    tasks: list[Task] = Field(default_factory=list)
    current_phase: str = "planning"
    status: str = "planning"       # planning / confirmed / executing / completed / failed
    chat_id: str = ""              # 飞书会话 ID
    user_message_id: str = ""      # 触发计划的飞书消息 ID
    creator_sender_id: str = ""    # P0 #5 修复：发起计划的发送者 open_id，用于横向越权防护
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    def get_task(self, task_id: str) -> Optional[Task]:
        """根据 ID 获取任务"""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_ready_tasks(self) -> list[Task]:
        """获取可以执行的任务（依赖已满足）"""
        completed_ids = {t.id for t in self.tasks if t.status == TaskStatus.COMPLETED}
        ready = []
        for task in self.tasks:
            if task.status == TaskStatus.CONFIRMED:
                if all(dep_id in completed_ids for dep_id in task.dependencies):
                    ready.append(task)
        return ready

    def get_running_tasks(self) -> list[Task]:
        """获取正在执行的任务"""
        return [t for t in self.tasks if t.status == TaskStatus.RUNNING]

    def is_all_done(self) -> bool:
        """是否所有任务都已完成"""
        return all(
            t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            for t in self.tasks
        )

    def get_progress_text(self) -> str:
        """生成进度报告文本"""
        lines = [f"📋 项目计划: {self.id}", f"需求: {self.requirement[:100]}...", ""]
        for task in self.tasks:
            lines.append(task.to_summary())

        total = len(self.tasks)
        done = sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in self.tasks if t.status == TaskStatus.FAILED)
        lines.append(f"\n进度: {done}/{total} 完成, {failed} 失败")
        return "\n".join(lines)

    def to_confirm_card(self) -> dict:
        """生成飞书确认卡片"""
        task_lines = []
        for i, task in enumerate(self.tasks, 1):
            dep_text = f" (依赖: {', '.join(task.dependencies)})" if task.dependencies else ""
            task_lines.append(f"{i}. **{task.title}** → {task.assigned_to}{dep_text}")

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📋 任务拆解方案"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**需求**: {self.requirement}\n\n**拆解结果**:\n" + "\n".join(task_lines),
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✅ 确认执行"},
                            "type": "primary",
                            "value": {"action": "confirm", "plan_id": self.id},
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "✏️ 需要修改"},
                            "type": "danger",
                            "value": {"action": "modify", "plan_id": self.id},
                        },
                    ],
                },
            ],
        }
        return card
