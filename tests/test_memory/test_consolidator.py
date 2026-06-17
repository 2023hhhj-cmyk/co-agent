"""MemoryConsolidator 单元测试 (2 个用例)"""

import pytest
from memory.consolidator import MemoryConsolidator


class TestMemoryConsolidator:
    """LLM 记忆提炼器"""

    def test_without_llm_returns_none(self):
        """LLM 不可用 → 静默返回 None"""
        consolidator = MemoryConsolidator(llm_client=None)
        assert not consolidator.is_ready

    def test_is_ready_with_llm(self):
        """注入 LLM → is_ready = True"""
        # 用 mock 对象模拟 LLM client
        class MockLLM:
            async def chat(self, *args, **kwargs):
                return '{"summary": "test", "key_decisions": [], "technical_notes": [], "outcome": "ok", "tags": ["test"]}'

        consolidator = MemoryConsolidator(llm_client=MockLLM())
        assert consolidator.is_ready
