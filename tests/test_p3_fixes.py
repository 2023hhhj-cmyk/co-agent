"""P3 #33 修复测试 — ProjectPlan 辅助方法"""

from datetime import datetime

import pytest

from models.task import ProjectPlan, Task, TaskStatus


def _make_plan() -> ProjectPlan:
    return ProjectPlan(
        requirement="test project",
        tasks=[
            Task(title="t1", description="x", assigned_to="developer", status=TaskStatus.PENDING),
            Task(title="t2", description="x", assigned_to="developer", status=TaskStatus.CONFIRMED),
            Task(title="t3", description="x", assigned_to="developer", status=TaskStatus.RUNNING),
            Task(title="t4", description="x", assigned_to="developer", status=TaskStatus.COMPLETED),
            Task(title="t5", description="x", assigned_to="developer", status=TaskStatus.FAILED),
            Task(title="t6", description="x", assigned_to="developer", status=TaskStatus.WAITING_DEP),
        ],
    )


class TestProjectPlanHelpers:
    """P3 #33 补充：明确的按状态查询方法"""

    def test_get_pending_tasks(self):
        """PENDING = 等用户确认"""
        plan = _make_plan()
        pending = plan.get_pending_tasks()
        assert len(pending) == 1
        assert pending[0].title == "t1"

    def test_get_running_tasks_accurate(self):
        """RUNNING = 正在执行（命名准确，无需重命名）"""
        plan = _make_plan()
        running = plan.get_running_tasks()
        assert len(running) == 1
        assert running[0].title == "t3"

    def test_get_waiting_tasks(self):
        """WAITING_DEP = 等待依赖"""
        plan = _make_plan()
        waiting = plan.get_waiting_tasks()
        assert len(waiting) == 1
        assert waiting[0].title == "t6"

    def test_get_ready_tasks_unchanged(self):
        """CONFIRMED + 依赖完成 = ready（原有行为保留）"""
        plan = _make_plan()
        ready = plan.get_ready_tasks()
        # t2 是 CONFIRMED 且无依赖
        assert len(ready) == 1
        assert ready[0].title == "t2"

    def test_count_by_status(self):
        """按状态统计"""
        plan = _make_plan()
        counts = plan.count_by_status()
        assert counts == {
            "pending": 1,
            "confirmed": 1,
            "running": 1,
            "completed": 1,
            "failed": 1,
            "waiting_dep": 1,
        }

    def test_count_by_status_empty_plan(self):
        """空计划的 count"""
        plan = ProjectPlan(requirement="empty")
        assert plan.count_by_status() == {}

    def test_progress_text_enhanced(self):
        """进度文本含运行/等待状态信息"""
        plan = _make_plan()
        text = plan.get_progress_text()
        assert "1/6 完成" in text
        assert "1 失败" in text
        assert "1 运行中" in text
        assert "1 等待依赖" in text
