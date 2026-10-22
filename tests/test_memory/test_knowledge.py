"""KnowledgeMemory 集成测试（需要 PostgreSQL 与 pgvector）"""

import pytest


@pytest.mark.asyncio
class TestKnowledgeMemory:
    """长期记忆 KnowledgeMemory"""

    @pytest.fixture(autouse=True)
    async def setup(self):
        from memory.knowledge import KnowledgeMemory
        from storage.database import Database

        self.km = KnowledgeMemory(database=Database())
        await self.km.setup()
        self.ready = self.km.is_ready
        yield

    async def test_add_and_semantic_search(self):
        """存储知识 → 用不同措辞检索 → 语义匹配成功"""
        if not self.ready:
            pytest.skip("PostgreSQL 或 pgvector 不可用")

        kid = await self.km.add_knowledge(
            "退款在7个工作日内处理",
            "test-km-01", "customer_service", "faq",
        )
        assert kid is not None

        results = await self.km.search("钱什么时候能退回来", n_results=3)
        assert len(results) > 0

    async def test_metadata_filter(self):
        """metadata 过滤检索"""
        if not self.ready:
            pytest.skip("PostgreSQL 或 pgvector 不可用")

        await self.km.add_knowledge(
            "React 项目推荐用 Vite 替代 CRA",
            "test-km-02", "developer", "best_practice",
        )

        results = await self.km.search(
            "前端构建工具", n_results=3,
            filter_metadata={"memory_type": "best_practice"},
        )
        assert len(results) > 0

    async def test_count(self):
        """集合条目计数"""
        if not self.ready:
            pytest.skip("PostgreSQL 或 pgvector 不可用")
        cnt = await self.km.count()
        assert cnt >= 0
