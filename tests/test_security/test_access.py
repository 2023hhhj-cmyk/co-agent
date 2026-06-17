"""PolicyEnforcer 单元测试 (5 个用例)"""

import pytest
from security.access_control import PolicyEnforcer


class TestPolicyEnforcer:
    """权限策略执行器"""

    def setup_method(self):
        self.ep = PolicyEnforcer()

    def test_ceo_can_watch_user_requirement(self):
        """CEO 可以 watch user_requirement"""
        assert self.ep.can_watch("ceo", {"user_requirement"})

    def test_developer_cannot_watch_feishu(self):
        """Developer 不能 watch feishu_message"""
        assert not self.ep.can_watch("developer", {"feishu_message"})

    def test_devops_can_deploy(self):
        """DevOps 可以 deploy"""
        assert self.ep.can_act("devops", "deploy")

    def test_cs_cannot_deploy(self):
        """客服不能 deploy"""
        assert not self.ep.can_act("customer_service", "deploy")

    def test_error_always_allowed(self):
        """所有角色可以发送 error"""
        for role in ["ceo", "developer", "devops", "finance", "customer_service"]:
            assert self.ep.validate_message_send(role, "error"), f"{role} should send error"
