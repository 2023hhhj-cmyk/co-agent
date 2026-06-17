"""WorkingMemory 单元测试 (6 个用例)"""

import pytest
from memory.working import WorkingMemory
from models.message import Message


class TestWorkingMemory:
    """短期记忆 WorkingMemory"""

    def test_add_and_capacity(self):
        """add 25 条 → 保留 20 条 (FIFO)"""
        wm = WorkingMemory(capacity=20)
        for i in range(25):
            wm.add(Message(content=f"msg-{i}", cause_by="test", sent_from="test"))
        assert len(wm) == 20
        assert "msg-5" in wm.get_all()[0].content  # FIFO 淘汰前5条

    def test_get_recent(self, sample_messages):
        """get_recent(k) 返回最近 k 条"""
        wm = WorkingMemory()
        for msg in sample_messages:
            wm.add(msg)
        assert len(wm.get_recent(3)) == 3
        assert len(wm.get_recent(100)) == 10  # 只有 10 条

    def test_get_text(self, sample_messages):
        """get_text 返回格式化文本"""
        wm = WorkingMemory()
        for msg in sample_messages:
            wm.add(msg)
        text = wm.get_text(3)
        assert "[test_agent]" in text
        assert "消息" in text

    def test_clear(self):
        """clear 后为空"""
        wm = WorkingMemory()
        wm.add(Message(content="test", cause_by="test", sent_from="test"))
        wm.clear()
        assert len(wm) == 0
        assert wm.is_empty

    def test_to_dict_list(self, sample_messages):
        """to_dict_list 导出为 dict 列表"""
        wm = WorkingMemory()
        for msg in sample_messages:
            wm.add(msg)
        result = wm.to_dict_list()
        assert len(result) == 10
        assert all("content" in d for d in result)
        assert all("sent_from" in d for d in result)

    def test_is_empty(self):
        """空记忆检查"""
        wm = WorkingMemory()
        assert wm.is_empty
        wm.add(Message(content="x", cause_by="test", sent_from="test"))
        assert not wm.is_empty
