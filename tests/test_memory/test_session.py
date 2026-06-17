"""SessionMemory 集成测试 (5 个用例，需要 Redis + PG)"""

import pytest


@pytest.mark.asyncio
class TestSessionMemory:
    """中期记忆 SessionMemory"""

    async def test_remember_and_recall_task(self, session_memory):
        """写入任务 → Redis 缓存 → 检索成功"""
        tid = await session_memory.remember_task(
            "test-session-01", "developer",
            {"content": "构建电商网站", "metadata": {"project": "ecommerce"}},
        )
        assert tid is not None

        tasks = await session_memory.recall_tasks("test-session-01", limit=10)
        assert any("电商" in t.get("content", "") for t in tasks)

    async def test_importance_score_in_metadata(self, session_memory):
        """记忆写入后 metadata 含 importance_score"""
        tid = await session_memory.remember_task(
            "test-session-02", "ceo",
            {"content": "战略决策：使用微服务架构"},
        )
        tasks = await session_memory.recall_tasks("test-session-02", limit=5)
        for t in tasks:
            meta = t.get("metadata", {})
            assert "importance_score" in meta, f"Missing importance_score in {meta}"

    async def test_chat_remember_and_recall(self, session_memory):
        """对话写入 → 检索成功"""
        await session_memory.remember_chat(
            "test-session-03", "customer_service", "chat-001",
            [{"role": "user", "content": "你好"}, {"role": "agent", "content": "您好"}],
        )
        chats = await session_memory.recall_chats("test-session-03", "chat-001")
        assert len(chats) >= 2

    async def test_redis_miss_pg_fallback(self, session_memory, state_manager):
        """Redis miss → PostgreSQL 回填"""
        tid = await session_memory.remember_task(
            "test-session-04", "developer",
            {"content": "Redis miss 测试内容"},
        )
        # 手动删除 Redis 缓存
        await state_manager.delete(f"agent:test-session-04:task:{tid}")

        tasks = await session_memory.recall_tasks("test-session-04", limit=10)
        assert any("Redis miss" in t.get("content", "") for t in tasks)

    async def test_get_single_task(self, session_memory):
        """get_task 单条查询"""
        tid = await session_memory.remember_task(
            "test-session-05", "developer",
            {"content": "单条查询测试"},
        )
        task = await session_memory.get_task("test-session-05", tid)
        assert task is not None
        assert "单条查询" in task["content"]
