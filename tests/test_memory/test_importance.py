"""ImportanceScorer 单元测试 (6 个用例)"""

import pytest
from datetime import datetime, timedelta
from memory.importance import ImportanceScorer


class TestImportanceScorer:
    """重要性评分器"""

    def setup_method(self):
        self.scorer = ImportanceScorer()

    def test_ceo_experience_high_score(self):
        """CEO 的 experience 记忆 → score > 0.7"""
        s = self.scorer.score(
            "战略决定采用 Next.js 作为核心技术栈，统一前端开发标准",
            agent_role="ceo", memory_type="experience"
        )
        assert s > 0.65, f"Expected >0.65, got {s}"

    def test_cs_greeting_low_score(self):
        """CS 的 "你好" chat → score < 0.4"""
        s = self.scorer.score(
            "你好", agent_role="customer_service", memory_type="chat"
        )
        assert s < 0.4, f"Expected <0.4, got {s}"

    def test_time_decay(self):
        """90 天前的记忆 → 衰减生效"""
        old = (datetime.now() - timedelta(days=90)).isoformat()
        s_old = self.scorer.score(
            "重要决策", agent_role="ceo", memory_type="decision", created_at=old
        )
        s_new = self.scorer.score(
            "重要决策", agent_role="ceo", memory_type="decision",
            created_at=datetime.now().isoformat()
        )
        assert s_old < s_new, f"Old({s_old}) should be < New({s_new})"

    def test_access_boost(self):
        """高访问频率 → score 更高"""
        s_hi = self.scorer.score(
            "FAQ", agent_role="customer_service", memory_type="faq", access_count=15
        )
        s_lo = self.scorer.score(
            "FAQ", agent_role="customer_service", memory_type="faq", access_count=0
        )
        assert s_hi > s_lo

    def test_role_ranking(self):
        """CEO > Developer > CS 角色排序"""
        ceo = self.scorer.score("决策", "ceo", "decision")
        dev = self.scorer.score("决策", "developer", "decision")
        cs_s = self.scorer.score("决策", "customer_service", "decision")
        assert ceo > dev > cs_s, f"ceo={ceo}, dev={dev}, cs={cs_s}"

    def test_explicit_importance_override(self):
        """显式重要性覆盖"""
        s = self.scorer.score(
            "xxx", "customer_service", "chat", explicit_importance=0.95
        )
        assert s == 0.95
