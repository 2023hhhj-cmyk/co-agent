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


class SubTaskStatus(str, Enum):
    """ponytail: 子任务状态机（v3 设计）。"""
    PENDING = "pending"      # 等待依赖
    READY = "ready"          # 依赖齐了，可跑
    RUNNING = "running"      # 跑中
    RETRYING = "retrying"    # 重试中
    SUCCESS = "success"      # 成功
    FAILED = "failed"        # 失败（重试完）
    SKIPPED = "skipped"      # 依赖失败 → 跳过

    @classmethod
    def can_transition(cls, src: "SubTaskStatus", dst: "SubTaskStatus") -> bool:
        """ponytail: 合法状态转移校验。"""
        valid = {
            cls.PENDING: {cls.READY, cls.SKIPPED},
            cls.READY: {cls.RUNNING, cls.SKIPPED},
            cls.RUNNING: {cls.SUCCESS, cls.FAILED, cls.RETRYING},
            cls.RETRYING: {cls.RUNNING, cls.FAILED},
            cls.SUCCESS: set(),
            cls.FAILED: set(),
            cls.SKIPPED: set(),
        }
        return dst in valid.get(src, set())


class SubTask:
    """ponytail: 一个可独立调 Claude Code 执行的子任务。"""

    __slots__ = (
        "name", "desc", "deps", "status", "output_path",
        "duration_ms", "error", "start_time", "retries",
    )

    def __init__(self, name: str, desc: str, deps: Optional[list] = None):
        self.name = name
        self.desc = desc
        self.deps = deps or []
        self.status = SubTaskStatus.PENDING
        self.output_path: Optional[str] = None
        self.duration_ms: int = 0
        self.error: Optional[str] = None
        self.start_time: float = 0.0
        self.retries: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "desc": self.desc,
            "deps": list(self.deps),
            "status": self.status.value,
            "output_path": self.output_path,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "retries": self.retries,
        }


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
        """获取可以执行的任务（依赖已满足 + 用户已确认）

        即：状态为 CONFIRMED 且所有依赖任务都已 COMPLETED 的任务。
        这些任务可被 WorkflowExecutor 立即派发。

        注意命名澄清（避免与"pending"混淆）：
          - PENDING  = 拆解完成，等用户确认
          - CONFIRMED = 用户已确认，等依赖完成
          - RUNNING  = 正在执行
          - WAITING_DEP = 等待依赖
        """
        completed_ids = {t.id for t in self.tasks if t.status == TaskStatus.COMPLETED}
        ready = []
        for task in self.tasks:
            if task.status == TaskStatus.CONFIRMED:
                if all(dep_id in completed_ids for dep_id in task.dependencies):
                    ready.append(task)
        return ready

    def get_running_tasks(self) -> list[Task]:
        """获取正在执行的任务（状态 = RUNNING）"""
        return [t for t in self.tasks if t.status == TaskStatus.RUNNING]

    def get_pending_tasks(self) -> list[Task]:
        """P3 #33 补充：获取待用户确认的任务（状态 = PENDING）

        之前只有 get_ready_tasks（CONFIRMED + 依赖完成），缺一个
        "等用户点头"的状态查询。本方法补足。
        """
        return [t for t in self.tasks if t.status == TaskStatus.PENDING]

    def get_waiting_tasks(self) -> list[Task]:
        """P3 #33 补充：获取等待依赖的任务（状态 = WAITING_DEP）"""
        return [t for t in self.tasks if t.status == TaskStatus.WAITING_DEP]

    def count_by_status(self) -> dict[str, int]:
        """P3 #33 补充：按状态统计任务数量

        Returns:
            例：{"pending": 2, "confirmed": 1, "running": 0, "completed": 3, "failed": 0, "waiting_dep": 1}
        """
        result: dict[str, int] = {}
        for task in self.tasks:
            key = task.status.value
            result[key] = result.get(key, 0) + 1
        return result

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

        # P3 #33 改进：使用 count_by_status 提供更丰富的进度信息
        counts = self.count_by_status()
        total = len(self.tasks)
        done = counts.get(TaskStatus.COMPLETED.value, 0)
        failed = counts.get(TaskStatus.FAILED.value, 0)
        running = counts.get(TaskStatus.RUNNING.value, 0)
        waiting = counts.get(TaskStatus.WAITING_DEP.value, 0)
        lines.append(
            f"\n进度: {done}/{total} 完成, {failed} 失败, "
            f"{running} 运行中, {waiting} 等待依赖"
        )
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
