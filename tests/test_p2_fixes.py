"""P2 修复测试 — 异常处理/限流/审计/部署监控"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===== P2 #17: 异常吞没修复 =====

class TestExceptionStackTraces:
    """P2 #17: 关键异常应包含 exc_info=True 以保留 stack trace"""

    def test_ceo_agent_exc_info_present(self):
        """CEO agent 的 logger.error 应带 exc_info"""
        with open("agents/ceo/agent.py") as f:
            source = f.read()

        # 关键错误日志应有 exc_info
        critical_loggers = [
            "意图分类失败",
            "需求完整性检查失败",
            "任务拆解失败",
            "工作流执行失败",
            "读取项目文件失败",
            "飞书发送失败",
            "项目记忆归档失败",
        ]
        for keyword in critical_loggers:
            # 找到该错误日志所在行
            idx = source.find(f'logger.error(f"{keyword}')
            if idx == -1:
                idx = source.find(f'logger.error(f"{keyword}:')
            assert idx != -1, f"未找到 {keyword} 日志"
            # 该行末尾应有 exc_info=True
            line_end = source.find("\n", idx)
            line = source[idx:line_end]
            assert "exc_info=True" in line, f"{keyword} 缺少 exc_info=True: {line}"


# ===== P2 #18: Redis AOF 检查 =====

class TestRedisAOFCheck:
    """P2 #18: 启动时检查 Redis AOF 配置"""

    def test_aof_check_in_lifespan(self):
        """lifespan 应有 AOF 检查逻辑"""
        with open("main.py") as f:
            source = f.read()
        assert "appendonly" in source, "lifespan 应检查 Redis appendonly 配置"
        assert "AOF" in source, "应有 AOF 相关注释"


# ===== P2 #19: 限流 X-Forwarded-For =====

class TestRateLimitXFF:
    """P2 #19: 限流支持 X-Forwarded-For"""

    def test_rate_limit_key_function_defined(self):
        """应有 _rate_limit_key 函数"""
        with open("main.py") as f:
            source = f.read()
        assert "def _rate_limit_key" in source, "_rate_limit_key 函数必须存在"
        assert "X-Forwarded-For" in source, "应读取 X-Forwarded-For 头"

    def test_limiter_uses_new_key_func(self):
        """Limiter 必须用 _rate_limit_key"""
        with open("main.py") as f:
            source = f.read()
        assert "Limiter(key_func=_rate_limit_key" in source

    def test_get_remote_address_not_directly_used(self):
        """get_remote_address 不应直接作为 key_func"""
        with open("main.py") as f:
            source = f.read()
        # 应该用 _rate_limit_key 包装 get_remote_address
        assert "Limiter(key_func=get_remote_address" not in source, \
            "P2 #19 修复：Limiter 不应直接用 get_remote_address"


# ===== P2 #20: LLM JSON 解析鲁棒性 =====

class TestLLMJSONParsing:
    """P2 #20: LLM 响应解析增强"""

    def test_extract_json_candidates_method_exists(self):
        """应有 _extract_json_candidates 辅助方法"""
        from tools.llm.client import LLMClient
        assert hasattr(LLMClient, "_extract_json_candidates")

    def test_extract_json_candidates_handles_plain_json(self):
        """纯 JSON 应作为第一个候选"""
        from tools.llm.client import LLMClient
        text = '{"key": "value"}'
        candidates = LLMClient._extract_json_candidates(text)
        assert text in candidates

    def test_extract_json_candidates_handles_markdown(self):
        """markdown 代码块包裹的 JSON 应被剥离"""
        from tools.llm.client import LLMClient
        text = '```json\n{"key": "value"}\n```'
        candidates = LLMClient._extract_json_candidates(text)
        # 剥离后的 JSON 应该在候选中
        joined = " ".join(candidates)
        assert '{"key": "value"}' in joined

    def test_extract_json_candidates_handles_embedded_json(self):
        """散文中的 JSON 应被提取"""
        from tools.llm.client import LLMClient
        text = 'Here is the answer: {"key": "value"} thanks!'
        candidates = LLMClient._extract_json_candidates(text)
        joined = " ".join(candidates)
        assert '{"key": "value"}' in joined

    def test_extract_json_candidates_handles_array(self):
        """数组形式 JSON 应被识别"""
        from tools.llm.client import LLMClient
        text = 'prefix [1, 2, 3] suffix'
        candidates = LLMClient._extract_json_candidates(text)
        joined = " ".join(candidates)
        assert "[1, 2, 3]" in joined

    @pytest.mark.asyncio
    async def test_chat_with_json_returns_parse_error_marker(self):
        """无法解析时返回 _parse_error 标记（不静默）"""
        from tools.llm.client import DeepSeekClient

        # 用具体子类（不是抽象基类）
        client = DeepSeekClient(api_key="fake", base_url="https://fake.com", model="fake")
        async def mock_chat(*args, **kwargs):
            return "not json at all, just text"

        with patch.object(client, "chat", side_effect=mock_chat):
            result = await client.chat_with_json([{"role": "user", "content": "test"}])

        assert "raw_response" in result
        assert "_parse_error" in result
        assert result["_parse_error"]  # 非空错误描述


# ===== P2 #21: 审计日志失败兜底 =====

class TestAuditFailureFallback:
    """P2 #21: 审计失败应保留事件不丢失"""

    def test_failed_events_attribute(self):
        """AuditLogger 应该有 _failed_events 属性"""
        from security.audit import AuditLogger
        al = AuditLogger()
        assert hasattr(al, "_failed_events")

    def test_retry_failed_events_method(self):
        """应该有 retry_failed_events 方法"""
        from security.audit import AuditLogger
        al = AuditLogger()
        assert hasattr(al, "retry_failed_events")
        assert callable(al.retry_failed_events)

    def test_get_failed_events_method(self):
        """应该有 get_failed_events 方法"""
        from security.audit import AuditLogger
        al = AuditLogger()
        assert hasattr(al, "get_failed_events")
        assert callable(al.get_failed_events)

    @pytest.mark.asyncio
    async def test_failed_event_added_on_db_error(self):
        """DB 写入失败时事件入 ring buffer"""
        from security.audit import AuditLogger

        # Mock DB 写入失败
        bad_db = MagicMock()
        bad_db.execute = AsyncMock(side_effect=Exception("DB down"))

        al = AuditLogger(database=bad_db)
        await al.log("test_action", "agent-1", {"key": "value"})

        # ring buffer 应有 1 条记录
        failed = al.get_failed_events()
        assert len(failed) == 1
        assert failed[0]["action"] == "test_action"
        assert "DB down" in failed[0]["error"]

    @pytest.mark.asyncio
    async def test_retry_failed_events_succeeds(self):
        """重试补偿写入应将事件从 ring buffer 移除"""
        from security.audit import AuditLogger

        # 先写失败
        bad_db = MagicMock()
        bad_db.execute = AsyncMock(side_effect=Exception("DB down"))
        al = AuditLogger(database=bad_db)
        await al.log("test_action", "agent-1", {"key": "value"})

        # 修复 DB，重试
        al.db.execute = AsyncMock(return_value=None)
        success_count = await al.retry_failed_events()
        assert success_count == 1
        assert len(al.get_failed_events()) == 0


# ===== P2 #22: Dockerfile 覆盖保护 =====

class TestDockerfileBackup:
    """P2 #22: 写入配置文件时先备份"""

    def test_safe_write_file_method_exists(self):
        """DevOpsAgent 应该有 _safe_write_file 静态方法"""
        from agents.devops.agent import DevOpsAgent
        assert hasattr(DevOpsAgent, "_safe_write_file")

    def test_safe_write_file_creates_new(self, tmp_path):
        """新文件应被创建（无备份）"""
        from agents.devops.agent import DevOpsAgent
        target = tmp_path / "Dockerfile"
        mode = DevOpsAgent._safe_write_file(str(target), "FROM python:3.12", ".bak")
        assert mode == "created"
        assert target.read_text() == "FROM python:3.12"
        # 不应有 .bak 文件
        assert list(tmp_path.glob("*.bak*")) == []

    def test_safe_write_file_backups_existing(self, tmp_path):
        """已存在的文件应先备份"""
        from agents.devops.agent import DevOpsAgent
        target = tmp_path / "Dockerfile"
        target.write_text("OLD CONTENT")

        mode = DevOpsAgent._safe_write_file(str(target), "NEW CONTENT", ".bak")
        assert mode == "updated_with_backup"
        assert target.read_text() == "NEW CONTENT"
        # 应有 .bak 文件
        bak_files = list(tmp_path.glob("Dockerfile.bak.*"))
        assert len(bak_files) == 1
        assert bak_files[0].read_text() == "OLD CONTENT"


# ===== P2 #23: 监控回调异常处理 =====

class TestMonitorCallbackSafety:
    """P2 #23: 监控回调异常不应导致循环退出"""

    def test_safe_invoke_callback_method_exists(self):
        """ServiceMonitor 应有 _safe_invoke_callback 辅助方法"""
        from tools.deploy.monitor import ServiceMonitor
        # 实例化需要 docker_manager
        dm = MagicMock()
        sm = ServiceMonitor(docker_manager=dm)
        assert hasattr(sm, "_safe_invoke_callback")

    @pytest.mark.asyncio
    async def test_safe_invoke_callback_handles_exception(self):
        """异常回调应被吞掉，不影响主流程"""
        from tools.deploy.monitor import ServiceMonitor

        sm = ServiceMonitor(docker_manager=MagicMock())

        async def bad_callback(*args):
            raise RuntimeError("callback crashed")

        # 不应抛异常
        await sm._safe_invoke_callback(bad_callback, "arg1")

    @pytest.mark.asyncio
    async def test_safe_invoke_callback_handles_sync_callback(self):
        """同步回调也应能调用"""
        from tools.deploy.monitor import ServiceMonitor

        sm = ServiceMonitor(docker_manager=MagicMock())

        def sync_callback(*args):
            return "ok"

        result = await sm._safe_invoke_callback(sync_callback, "arg1")
        # 同步回调不应抛错
        assert result is None  # _safe_invoke_callback 不返回

    @pytest.mark.asyncio
    async def test_safe_invoke_callback_handles_async_callback(self):
        """异步回调也应能调用"""
        from tools.deploy.monitor import ServiceMonitor

        sm = ServiceMonitor(docker_manager=MagicMock())

        async def async_callback(*args):
            return "ok"

        # 不应抛异常
        await sm._safe_invoke_callback(async_callback, "arg1")

    @pytest.mark.asyncio
    async def test_safe_invoke_callback_handles_none(self):
        """None 回调（未设置）应直接返回"""
        from tools.deploy.monitor import ServiceMonitor

        sm = ServiceMonitor(docker_manager=MagicMock())
        # 不应抛异常
        await sm._safe_invoke_callback(None)


# ===== P2 #24: lifespan 容错 =====

class TestLifespanTolerance:
    """P2 #24: lifespan 各子系统失败不应阻塞 app 启动"""

    def test_sandbox_creation_wrapped(self):
        """Sandbox 初始化应包 try/except"""
        with open("main.py") as f:
            source = f.read()
        # 找到 sandbox = SandboxedExecutor 周围
        idx = source.find("sandbox = SandboxedExecutor")
        assert idx != -1
        # 向上找 try:
        before = source[max(0, idx - 500):idx]
        assert "try:" in before, "Sandbox 初始化应包在 try 中"
        # 向下找 except
        after = source[idx:idx + 1000]
        assert "except" in after, "Sandbox 初始化后应有 except"

    def test_create_agents_wrapped(self):
        """create_agents 应包 try/except（在 lifespan 内）"""
        with open("main.py") as f:
            source = f.read()
        # 找到 lifespan 内的 create_agents() 调用
        # lifespan 内的上下文在 600-700 行附近
        idx = source.find("create_agents()")
        assert idx != -1, "create_agents() 必须存在"
        # 向前看 200 字符，应有 try
        # 由于 main.py 顶部有 def create_agents(): 函数定义，调用在 lifespan 内
        # 通过查找 lifespan 函数内的 create_agents() 调用
        # 简化：找 lifespan 函数定义
        lifespan_idx = source.find("async def lifespan")
        assert lifespan_idx != -1
        # 在 lifespan 函数内查找 create_agents() 调用
        lifespan_end = source.find("yield", lifespan_idx)
        lifespan_body = source[lifespan_idx:lifespan_end]
        # 在 lifespan 内，create_agents() 周围应有 try
        call_idx = lifespan_body.find("create_agents()")
        assert call_idx != -1, "lifespan 必须调用 create_agents()"
        before = lifespan_body[max(0, call_idx - 300):call_idx]
        assert "try:" in before, "lifespan 内 create_agents() 应包 try"
        after = lifespan_body[call_idx:call_idx + 1000]
        assert "except" in after, "lifespan 内 create_agents() 后应有 except"


# ===== P2 #25: webhook 幂等性 =====

class TestWebhookIdempotency:
    """P2 #25: 所有 webhook 入口都应做去重"""

    def test_ceo_dedup(self):
        """Wecom CEO webhook dedup"""
        with open("main.py") as f:
            source = f.read()
        assert "wecom_ceo:" in source, "CEO webhook 应有 dedup key prefix"

    def test_cs_dedup(self):
        """Wecom CS webhook dedup"""
        with open("main.py") as f:
            source = f.read()
        assert "wecom_cs:" in source, "CS webhook 应有 dedup key prefix"

    def test_feishu_dedup(self):
        """Feishu webhook dedup"""
        with open("main.py") as f:
            source = f.read()
        assert "feishu_msg:" in source, "飞书 webhook 应有 dedup key prefix"
