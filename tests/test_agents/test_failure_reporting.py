"""Regression checks for exception preservation and truthful workflow reports."""
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

from agents.base import BaseAgent
from agents.ceo.agent import CEOAgent
from models.agent import AgentRole
from models.message import Message
from models.task import ProjectPlan, Task, TaskStatus


class MinimalAgent(BaseAgent):
    async def _think(self, messages):
        return None

    async def _act(self, action, context=None):
        return Message(content="done")


@pytest.mark.parametrize("failure_stage", ["execution", "publication", None])
@pytest.mark.parametrize("audit_fails", [False, True])
async def test_agent_outcome_metrics_and_original_exception(failure_stage, audit_fails):
    audit = MagicMock()
    audit.log_task_executed = AsyncMock(
        side_effect=RuntimeError("audit unavailable") if audit_fails else None
    )
    agent = MinimalAgent(AgentRole(name="regression"), audit_logger=audit)
    original = ValueError("original task error")
    agent._react = AsyncMock(return_value=Message(content="done"))
    agent.publish_message = AsyncMock()
    if failure_stage == "execution":
        agent._react.side_effect = original
    elif failure_stage == "publication":
        agent.publish_message.side_effect = original

    with patch("agents.base.task_total") as counter:
        if failure_stage:
            with pytest.raises(ValueError) as caught:
                await agent.run(Message(content="task"))
            assert caught.value is original
        else:
            assert (await agent.run(Message(content="task"))).content == "done"
        counter.labels.assert_called_once_with(
            agent="regression", status="error" if failure_stage else "success"
        )
        counter.labels.return_value.inc.assert_called_once()
    assert agent.state == "idle"
    assert audit.log_task_executed.await_args.kwargs["result"] == (
        "error" if failure_stage else "success"
    )


def make_ceo():
    ceo = CEOAgent.__new__(CEOAgent)
    ceo._send_feishu_text = AsyncMock()
    ceo._archive_project_memory = AsyncMock()
    ceo._clear_active_plan = AsyncMock()
    return ceo


@pytest.mark.parametrize("failures, status, heading", [
    (0, "completed", "🎉 项目完成！"),
    (1, "partial", "⚠️ 项目部分完成"),
    (2, "failed", "❌ 项目执行失败"),
])
async def test_real_workflow_failures_reach_plan_and_report(failures, status, heading):
    ceo = make_ceo()
    tasks = [Task(title=f"task-{i}", description="run", assigned_to=f"role-{i}")
             for i in range(2)]
    plan = ProjectPlan(requirement="test", tasks=tasks)
    ceo.team = {}
    for i, task in enumerate(tasks):
        ceo.team[task.assigned_to] = SimpleNamespace(run=AsyncMock(
            side_effect=ValueError(f"failure-{i}") if i < failures else None,
            return_value=Message(content=f"output-{i}"),
        ))

    await ceo._execute_plan("chat", plan)

    assert plan.status == status
    assert plan.completed_at is not None
    for i, task in enumerate(plan.tasks):
        assert task.status == (TaskStatus.FAILED if i < failures else TaskStatus.COMPLETED)
        if i < failures:
            assert task.error == f"failure-{i}"
        ceo.team[task.assigned_to].run.assert_awaited_once()
    report = ceo._send_feishu_text.await_args.args[1]
    assert heading in report
    if failures:
        assert "failure-0" in report
        assert "🎉 项目完成！" not in report
    ceo._archive_project_memory.assert_awaited_once_with("chat", plan)
    ceo._clear_active_plan.assert_awaited_once_with("chat")


async def test_invalid_graph_marks_unstarted_tasks_failed():
    ceo = make_ceo()
    plan = ProjectPlan(requirement="test", tasks=[
        Task(title="first", description="run", assigned_to="developer", dependencies=["second"]),
        Task(title="second", description="run", assigned_to="developer", dependencies=["first"]),
    ])
    await ceo._execute_plan("chat", plan)
    assert plan.status == "failed"
    assert all(t.status == TaskStatus.FAILED for t in plan.tasks)
    assert all("cycles" in t.error for t in plan.tasks)
    assert "❌ 项目执行失败" in ceo._send_feishu_text.await_args.args[1]


@pytest.mark.parametrize("fail", [False, True])
async def test_failed_dependency_skips_descendants_and_preserves_independent_branch(fail):
    ceo = make_ceo()
    # A -> B -> C; D -> E; F requires both A and D.
    specs = [("A", []), ("B", ["A"]), ("C", ["B"]),
             ("D", []), ("E", ["D"]), ("F", ["A", "D"])]
    plan = ProjectPlan(requirement="branches", tasks=[
        Task(id=name, title=name, description=f"task {name}",
             assigned_to=name, dependencies=deps)
        for name, deps in specs
    ])
    ceo.team = {
        name: SimpleNamespace(run=AsyncMock(
            side_effect=ValueError("A failed") if fail and name == "A" else None,
            return_value=Message(content=f"result {name}"),
        ))
        for name, _ in specs
    }
    with patch("workflows.executor.emit_event") as events:
        await ceo._execute_plan("chat", plan)

    skipped = {"B", "C", "F"} if fail else set()
    for task in plan.tasks:
        if task.id in skipped:
            ceo.team[task.id].run.assert_not_awaited()
            assert task.status == TaskStatus.SKIPPED
            assert "前置任务" in task.error
        else:
            ceo.team[task.id].run.assert_awaited_once()
            assert task.status == (TaskStatus.FAILED if fail and task.id == "A"
                                   else TaskStatus.COMPLETED)
    assert plan.is_all_done()
    assert plan.status == ("partial" if fail else "completed")
    report = ceo._send_feishu_text.await_args.args[1]
    assert ("2 完成, 1 失败, 3 跳过" if fail else "6 完成, 0 失败, 0 跳过") in report
    if fail:
        assert "跳过原因" in report
        assert "3 跳过" in plan.get_progress_text()
    end = [c.kwargs for c in events.call_args_list if c.args[0] == "workflow.end"]
    assert len(end) == 1
    assert end[0]["success_nodes"] == (2 if fail else 6)
    assert end[0]["status"] == ("partial" if fail else "success")
    # Successful progress notifications must never be emitted for skipped nodes.
    notices = [c.args[1] for c in ceo._send_feishu_text.await_args_list]
    for name in skipped:
        assert f"✅ 任务 [{name}] 已完成" not in notices

async def test_agent_fork_keeps_shared_services_and_isolates_task_state():
    agent = MinimalAgent(AgentRole(name="regression"))
    first = agent.fork("client-a")
    second = agent.fork("client-b")
    assert first is not agent and second is not first
    assert first.agent_id != second.agent_id
    assert first.memory_owner_id != second.memory_owner_id
    assert first.role is agent.role
    first.memory.append(Message(content="client-a"))
    assert second.memory == [] and agent.memory == []
    assert first.message_queue is not second.message_queue


async def test_executor_forks_agent_for_customer_before_running():
    from workflows.converter import PlanToGraphConverter
    from workflows.executor import WorkflowExecutor
    from workflows.graph import WorkflowGraph

    task = Task(title="work", description="run", assigned_to="developer")
    graph = WorkflowGraph(PlanToGraphConverter().convert(ProjectPlan(requirement="test", tasks=[task])))
    child = SimpleNamespace(run=AsyncMock(return_value=Message(content="done")))
    parent = SimpleNamespace(fork=MagicMock(return_value=child), run=AsyncMock())
    result = await WorkflowExecutor(graph, {"developer": parent}).execute(
        Message(content="test"), extra_context={"client_id": "customer-123"}
    )
    parent.fork.assert_called_once_with("customer-123")
    parent.run.assert_not_awaited()
    child.run.assert_awaited_once()
    assert result["outputs"][task.id]["content"] == "done"
