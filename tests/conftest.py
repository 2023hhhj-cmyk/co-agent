"""
Pytest 共享 fixtures

使用方式 (自动发现，无需手动 import):
  async def test_something(session_memory):  # fixture 自动注入
"""

import asyncio
import pytest
from models.message import Message


# ========== 纯单元测试 fixtures (无需外部服务) ==========

@pytest.fixture
def sample_message():
    """创建一个测试消息"""
    return Message(
        content="测试消息内容",
        cause_by="test",
        sent_from="test_agent",
    )


@pytest.fixture
def sample_messages():
    """创建多条测试消息"""
    return [
        Message(content=f"消息 {i}", cause_by="test", sent_from="test_agent")
        for i in range(10)
    ]


# ========== 集成测试 fixtures (需要外部服务) ==========
# 这些在用 --integration 标记时使用

@pytest.fixture
async def state_manager():
    """创建 Redis 状态管理器连接"""
    from storage.state_manager import StateManager
    sm = StateManager()
    await sm.connect()
    yield sm
    await sm.disconnect()


@pytest.fixture
async def database():
    """创建数据库连接"""
    from storage.database import Database
    db = Database()
    await db.connect()
    yield db
    # 清理测试数据
    await db.execute("DELETE FROM memories WHERE agent_id LIKE 'test-%'")
    await db.execute("DELETE FROM audit_logs WHERE agent_id LIKE 'test-%'")
    await db.execute("DELETE FROM memory_edges WHERE source_id LIKE 'mem-%' OR source_id LIKE 'test-%'")
    await db.disconnect()


@pytest.fixture
async def session_memory(state_manager, database):
    """创建 SessionMemory 实例"""
    from memory.session import SessionMemory
    return SessionMemory(state_manager=state_manager, database=database)


@pytest.fixture
async def memory_manager(session_memory):
    """创建 MemoryManager 实例（不含 KnowledgeMemory，用于单元逻辑测试）"""
    from memory.manager import MemoryManager
    return MemoryManager(session_memory=session_memory, knowledge_memory=None)
