"""CEO Agent 卡片回调横向越权防护测试 — 覆盖 P0 #5"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.task import ProjectPlan, Task
from agents.ceo.agent import CEOAgent


# ========== P0 #5 修复测试 ==========

def _make_ceo_agent() -> CEOAgent:
    """构造一个最小可用的 CEOAgent（mock 掉所有外部依赖）"""
    # 不走 __init__，直接构造，避免触发 BaseAgent 的 memory_manager / message_bus 注册
    agent = CEOAgent.__new__(CEOAgent)
    agent.agent_id = "test-agent-id"
    agent._active_plans = {}
    agent._current_sender_id = ""
    agent._current_chat_id = ""
    agent._current_chat_type = "p2p"
    agent.feishu = MagicMock()
    agent.input_guard = None
    agent.audit_logger = None
    return agent


def _make_plan(creator_sender: str = "ou_creator_abc") -> ProjectPlan:
    return ProjectPlan(
        requirement="做一个官网",
        tasks=[Task(title="设计", description="x", assigned_to="developer")],
        chat_id="oc_chat_123",
        user_message_id="om_msg_999",
        creator_sender_id=creator_sender,
        status="planning",
        created_at=datetime.now(),
    )


class TestCEOCardCallbackLateralAuth:
    """P0 #5: 横向越权防护

    攻击场景：群聊中，chat_id 共享。
    任何群成员伪造 confirm 回调（action=confirm, plan_id=<A's plan>）
    都能触发 A 的项目执行。
    修复：回调时验证 sender_open_id == plan.creator_sender_id
    """

    @pytest.mark.asyncio
    async def test_creator_can_confirm(self):
        """创建者本人 confirm → 通过"""
        ceo = _make_ceo_agent()
        plan = _make_plan(creator_sender="ou_creator_abc")
        ceo._active_plans["oc_chat_123"] = plan
        ceo._current_sender_id = "ou_creator_abc"

        # Mock 实际执行和发送
        with patch.object(ceo, "_execute_plan", new=AsyncMock()) as mock_exec, \
             patch.object(ceo, "_send_feishu_text", new=AsyncMock()) as mock_send:
            await ceo._handle_card_callback(
                chat_id="oc_chat_123",
                action="confirm",
                plan_id=plan.id,
                message_id="om_callback_1",
            )

        mock_exec.assert_called_once()
        mock_send.assert_not_called()  # 拒绝时才会 send_feishu_text

    @pytest.mark.asyncio
    async def test_non_creator_blocked_from_confirm(self):
        """非创建者 confirm → 拒绝（横向越权拦截）"""
        ceo = _make_ceo_agent()
        plan = _make_plan(creator_sender="ou_creator_abc")
        ceo._active_plans["oc_chat_123"] = plan
        # 群聊中另一个用户尝试 confirm A 的计划
        ceo._current_sender_id = "ou_attacker_xyz"

        with patch.object(ceo, "_execute_plan", new=AsyncMock()) as mock_exec, \
             patch.object(ceo, "_send_feishu_text", new=AsyncMock()) as mock_send, \
             patch.object(ceo, "audit_logger", new=MagicMock()) as mock_audit:
            mock_audit.log_auth_failure = AsyncMock()

            await ceo._handle_card_callback(
                chat_id="oc_chat_123",
                action="confirm",
                plan_id=plan.id,
                message_id="om_attack_1",
            )

        mock_exec.assert_not_called()
        # 应给用户回复拒绝消息
        mock_send.assert_called_once()
        reject_msg = mock_send.call_args[0][1]
        assert "发起人" in reject_msg or "⛔" in reject_msg
        # 应记录审计日志
        mock_audit.log_auth_failure.assert_called_once()
        call_args = mock_audit.log_auth_failure.call_args
        assert "lateral_auth" in call_args.kwargs.get("action", "")

    @pytest.mark.asyncio
    async def test_non_creator_blocked_from_modify(self):
        """非创建者 modify → 拒绝"""
        ceo = _make_ceo_agent()
        plan = _make_plan(creator_sender="ou_creator_abc")
        ceo._active_plans["oc_chat_123"] = plan
        ceo._current_sender_id = "ou_attacker_xyz"

        with patch.object(ceo, "_send_feishu_text", new=AsyncMock()) as mock_send:
            await ceo._handle_card_callback(
                chat_id="oc_chat_123",
                action="modify",
                plan_id=plan.id,
                message_id="om_attack_2",
            )

        # modify 路径应被拒绝
        assert plan.status == "planning", "非创建者 modify 不应改变 plan 状态"
        mock_send.assert_called_once()  # 拒绝消息

    @pytest.mark.asyncio
    async def test_missing_creator_allows_for_backward_compat(self):
        """历史计划 creator_sender_id 为空 → 放行（向后兼容）

        早期创建的 plan 没有 creator 记录。不阻断是为了避免
        阻塞老用户；新计划都已记录 sender。
        """
        ceo = _make_ceo_agent()
        plan = _make_plan(creator_sender="")  # 空
        ceo._active_plans["oc_chat_123"] = plan
        ceo._current_sender_id = "ou_anyone"

        with patch.object(ceo, "_execute_plan", new=AsyncMock()) as mock_exec, \
             patch.object(ceo, "_send_feishu_text", new=AsyncMock()):
            await ceo._handle_card_callback(
                chat_id="oc_chat_123",
                action="confirm",
                plan_id=plan.id,
                message_id="om_legacy_1",
            )

        # 老计划无 creator 记录，应放行（兼容）
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_current_sender_allows_for_safety(self):
        """回调本身缺少 sender（系统问题）→ 放行但记录日志

        保守策略：sender 缺失时（如 webhook 解析失败）不阻断用户，
        但应记录告警。这比误伤好。
        """
        ceo = _make_ceo_agent()
        plan = _make_plan(creator_sender="ou_creator_abc")
        ceo._active_plans["oc_chat_123"] = plan
        ceo._current_sender_id = ""  # 缺失

        with patch.object(ceo, "_execute_plan", new=AsyncMock()) as mock_exec, \
             patch.object(ceo, "_send_feishu_text", new=AsyncMock()):
            await ceo._handle_card_callback(
                chat_id="oc_chat_123",
                action="confirm",
                plan_id=plan.id,
                message_id="om_no_sender_1",
            )

        # 缺失 sender 不阻断（避免误伤）
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_plan_id_mismatch_rejected(self):
        """plan_id 不匹配 → 拒绝（防止用其他 plan 替换）"""
        ceo = _make_ceo_agent()
        plan = _make_plan(creator_sender="ou_creator_abc")
        ceo._active_plans["oc_chat_123"] = plan
        ceo._current_sender_id = "ou_creator_abc"

        with patch.object(ceo, "_execute_plan", new=AsyncMock()) as mock_exec, \
             patch.object(ceo, "_send_feishu_text", new=AsyncMock()) as mock_send:
            await ceo._handle_card_callback(
                chat_id="oc_chat_123",
                action="confirm",
                plan_id="DIFFERENT_PLAN_ID",  # 伪造
                message_id="om_fake_1",
            )

        mock_exec.assert_not_called()
        mock_send.assert_called_once()  # "未找到对应的项目计划"


class TestProjectPlanCreatorField:
    """P0 #5: ProjectPlan.creator_sender_id 字段必须存在"""

    def test_field_exists(self):
        """ProjectPlan 必须有 creator_sender_id 字段"""
        plan = ProjectPlan(
            requirement="test",
            tasks=[],
            chat_id="oc_x",
            user_message_id="om_x",
            creator_sender_id="ou_abc",
        )
        assert hasattr(plan, "creator_sender_id")
        assert plan.creator_sender_id == "ou_abc"

    def test_field_default_empty(self):
        """默认值为空（向后兼容老数据）"""
        plan = ProjectPlan(requirement="test", tasks=[])
        assert plan.creator_sender_id == ""

    def test_field_serializable(self):
        """model_dump 必须包含 creator_sender_id（持久化兼容）"""
        plan = ProjectPlan(
            requirement="test",
            tasks=[],
            creator_sender_id="ou_xyz",
        )
        d = plan.model_dump()
        assert d.get("creator_sender_id") == "ou_xyz"
