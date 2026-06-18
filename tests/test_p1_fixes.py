"""P1 修复测试 — 覆盖 main.py / sandbox / message_bus / state_manager 改动"""

import asyncio
import json
import os
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ===== P1 #8: ContextVar 隔离 =====

class TestCEOSharedStateContextVar:
    """P1 #8: 共享状态改用 contextvars，避免并发串扰"""

    def test_context_vars_module_level(self):
        """ContextVar 应在模块级别定义"""
        from agents.ceo import agent as ceo_mod
        # ContextVar 类型
        import contextvars
        assert isinstance(ceo_mod._current_sender_id, contextvars.ContextVar)
        assert isinstance(ceo_mod._current_chat_id, contextvars.ContextVar)
        assert isinstance(ceo_mod._current_chat_type, contextvars.ContextVar)

    @pytest.mark.asyncio
    async def test_concurrent_set_does_not_overwrite(self):
        """并发 set 不同 chat 上下文应隔离

        模拟两个协程同时 set 不同的 sender_id，验证它们互不影响。
        """
        from agents.ceo import agent as ceo_mod

        results = {}

        async def task_a():
            ceo_mod._current_sender_id.set("user_A")
            await asyncio.sleep(0.05)  # 让 B 有机会运行
            results["A"] = ceo_mod._current_sender_id.get()

        async def task_b():
            ceo_mod._current_sender_id.set("user_B")
            await asyncio.sleep(0.05)
            results["B"] = ceo_mod._current_sender_id.get()

        await asyncio.gather(task_a(), task_b())

        assert results["A"] == "user_A", "A 协程的 sender_id 不应被 B 覆盖"
        assert results["B"] == "user_B", "B 协程的 sender_id 应独立"


# ===== P1 #9: MessageBus 持久化 =====

class TestMessageBusPersistence:
    """P1 #9: MessageBus 可选 Redis 持久化"""

    @pytest.mark.asyncio
    async def test_publish_without_state_manager(self):
        """无 state_manager 时仍正常工作（仅内存模式）"""
        from communication.message_bus import MessageBus
        from models.message import Message

        bus = MessageBus()  # 无 state_manager
        bus.register_agent("agent_1", watch={"test"})

        msg = Message(content="hello", cause_by="test", sent_from="other")
        await bus.publish(msg)

        messages = await bus.get_messages("agent_1")
        assert len(messages) == 1
        assert messages[0].content == "hello"

    @pytest.mark.asyncio
    async def test_publish_with_state_manager_persists(self):
        """有 state_manager 时持久化到 Redis（fire-and-forget）"""
        from communication.message_bus import MessageBus
        from models.message import Message

        # Mock state_manager
        sm = MagicMock()
        sm.lpush = AsyncMock()
        sm.ltrim = AsyncMock()
        sm.expire = AsyncMock()

        bus = MessageBus(state_manager=sm)
        bus.register_agent("agent_1", watch={"test"})

        msg = Message(content="persisted", cause_by="test", sent_from="other")
        await bus.publish(msg)

        # 等异步 fire-and-forget 完成
        await asyncio.sleep(0.1)

        # 验证 Redis 写入
        sm.lpush.assert_called()
        sm.ltrim.assert_called()
        sm.expire.assert_called()

    @pytest.mark.asyncio
    async def test_persisted_history_recovery(self):
        """从 Redis 恢复历史消息"""
        from communication.message_bus import MessageBus
        from models.message import Message

        # Mock state_manager 模拟 Redis 中已有数据
        sm = MagicMock()
        sm.lrange = AsyncMock(return_value=[
            json.dumps({"id": "1", "content": "old_msg", "cause_by": "test", "sent_from": "x", "send_to": ["*"], "role": "assistant", "metadata": {}}),
        ])

        bus = MessageBus(state_manager=sm)
        history = await bus.get_persisted_history(limit=10)
        assert len(history) == 1
        assert history[0]["content"] == "old_msg"


# ===== P1 #10: message_id 唯一性 =====

class TestCardCallbackMessageId:
    """P1 #10: 卡片回调 message_id 改用 uuid，避免高并发撞车"""

    def test_message_id_includes_uuid_not_time(self):
        """检查源码使用 uuid 而非 time()"""
        # 直接读 main.py 文件源码（避免 inspect 限制）
        with open("main.py") as f:
            source = f.read()
        # 应该出现 uuid.uuid4
        assert "uuid.uuid4" in source, "必须使用 uuid.uuid4 生成 message_id"
        # 不能再用 int(time())
        assert "int(asyncio.get_event_loop().time())" not in source, \
            "P1 #10 修复：不能再用 int(time()) 生成 message_id"

    def test_uuid_import_present(self):
        """main.py 应 import uuid"""
        with open("main.py") as f:
            source = f.read()
        assert "import uuid" in source


# ===== P1 #11: DevOps 路径校验 =====

class TestDevOpsProjectPathValidation:
    """P1 #11: 部署路径白名单"""

    def test_whitelist_prefix_defined(self):
        """类应有 _ALLOWED_PROJECT_DIR_PREFIX 常量"""
        from agents.devops.agent import DevOpsAgent
        assert hasattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX")
        # 默认 /root/，但可被 MUZHU_PROJECT_BASE 环境变量覆盖（用于测试）
        assert DevOpsAgent._ALLOWED_PROJECT_DIR_PREFIX in ("/root/", "/tmp/")

    def test_validate_project_dir_accepts_safe_path(self, monkeypatch, tmp_path):
        """合规路径应通过（用 MUZHU_PROJECT_BASE 注入测试根）"""
        import os
        from agents.devops.agent import DevOpsAgent

        # 临时覆盖白名单基址
        test_base = tmp_path / "muzhu_base"
        test_base.mkdir()
        monkeypatch.setattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX", str(test_base) + "/")

        client_dir = test_base / "client-x"
        client_dir.mkdir()

        result = DevOpsAgent._validate_project_dir(str(client_dir), "client-x")
        assert result == os.path.realpath(str(client_dir))

    def test_validate_project_dir_rejects_etc(self, monkeypatch, tmp_path):
        """攻击路径 /etc 应被拒绝"""
        from agents.devops.agent import DevOpsAgent
        test_base = tmp_path / "muzhu_base"
        test_base.mkdir()
        monkeypatch.setattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX", str(test_base) + "/")
        with pytest.raises(ValueError, match="不在白名单"):
            DevOpsAgent._validate_project_dir("/etc/passwd", "attacker")

    def test_validate_project_dir_rejects_root(self, monkeypatch, tmp_path):
        """根目录 / 应被拒绝"""
        from agents.devops.agent import DevOpsAgent
        test_base = tmp_path / "muzhu_base"
        test_base.mkdir()
        monkeypatch.setattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX", str(test_base) + "/")
        with pytest.raises(ValueError, match="不在白名单"):
            DevOpsAgent._validate_project_dir("/", "attacker")

    def test_validate_project_dir_rejects_other_prefix(self, monkeypatch, tmp_path):
        """非白名单基址路径应拒绝"""
        from agents.devops.agent import DevOpsAgent
        test_base = tmp_path / "muzhu_base"
        test_base.mkdir()
        monkeypatch.setattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX", str(test_base) + "/")
        with pytest.raises(ValueError, match="不在白名单"):
            DevOpsAgent._validate_project_dir("/tmp/whatever", "client-x")

    def test_validate_project_dir_rejects_wrong_subdir(self, monkeypatch, tmp_path):
        """基址下其他子目录应拒绝（必须落在 safe_name/ 下）"""
        from agents.devops.agent import DevOpsAgent
        test_base = tmp_path / "muzhu_base"
        test_base.mkdir()
        monkeypatch.setattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX", str(test_base) + "/")
        other = test_base / "evil"
        other.mkdir()
        with pytest.raises(ValueError, match="不在白名单"):
            DevOpsAgent._validate_project_dir(str(other), "client-x")

    def test_validate_project_dir_rejects_path_traversal(self, monkeypatch, tmp_path):
        """路径遍历攻击应被拒绝"""
        from agents.devops.agent import DevOpsAgent
        test_base = tmp_path / "muzhu_base"
        test_base.mkdir()
        monkeypatch.setattr(DevOpsAgent, "_ALLOWED_PROJECT_DIR_PREFIX", str(test_base) + "/")
        client_dir = test_base / "client-x"
        client_dir.mkdir()
        # 试图通过 ../ 跳到 test_base/elsewhere
        with pytest.raises(ValueError, match="不在白名单"):
            DevOpsAgent._validate_project_dir(str(client_dir / ".." / "elsewhere"), "client-x")

    def test_validate_project_dir_rejects_empty(self):
        """空路径应被拒绝"""
        from agents.devops.agent import DevOpsAgent
        with pytest.raises(ValueError, match="为空"):
            DevOpsAgent._validate_project_dir("", "client-x")
        with pytest.raises(ValueError, match="为空"):
            DevOpsAgent._validate_project_dir(None, "client-x")  # type: ignore


# ===== P1 #12 + #13: Sandbox 沙箱加固 =====

class TestSandboxHardening:
    """P1 #12 + #13: 沙箱 tmpfs noexec + 回退模式只读"""

    def test_tmpfs_noexec(self):
        """源码中 tmpfs 必须 noexec"""
        import inspect
        from security.sandbox import SandboxedExecutor
        source = inspect.getsource(SandboxedExecutor._execute_docker)
        assert "noexec" in source, "P1 #12 修复：tmpfs 必须 noexec"
        assert "rw,exec" not in source, "P1 #12 修复：禁止 rw,exec"
        # 必须有 no-new-privileges
        assert "no-new-privileges" in source, "P1 #12 修复：必须加 no-new-privileges"

    def test_fallback_tools_no_bash(self):
        """P1 #13：回退模式不应包含 Bash（合法工具名）"""
        from security.sandbox import READONLY_TOOLS
        # 不应该有任何 Bash 类工具
        bash_tools = [t for t in READONLY_TOOLS if "Bash" in t]
        assert bash_tools == [], f"P1 #13 修复：回退模式不应包含 Bash 工具，实际: {bash_tools}"

    def test_fallback_tools_still_has_read_glob_grep(self):
        """回退模式保留只读工具"""
        from security.sandbox import READONLY_TOOLS
        assert "Read" in READONLY_TOOLS
        assert "Glob" in READONLY_TOOLS
        assert "Grep" in READONLY_TOOLS

    def test_fallback_full_tools_unchanged(self):
        """完整工具集（Docker 模式）保持不变"""
        from security.sandbox import FULL_TOOLS
        assert "Bash" in FULL_TOOLS  # Docker 模式允许 Bash（受沙箱约束）


# ===== P1 #14: Webhook body 大小限制 =====

class TestBodySizeLimit:
    """P1 #14: webhook body size 限制（5MB）"""

    def test_max_body_size_constant(self):
        """MAX_BODY_SIZE 常量应存在"""
        from main import app
        # 通过模块级变量查找
        import main
        assert hasattr(main, "MAX_BODY_SIZE")
        assert main.MAX_BODY_SIZE == 5 * 1024 * 1024

    def test_middleware_registered(self):
        """body_size_limit_middleware 应注册到 app"""
        # 直接读 main.py 源码
        with open("main.py") as f:
            source = f.read()
        assert "body_size_limit_middleware" in source, \
            "main.py 必须定义 body_size_limit_middleware"
        assert "app.middleware" in source and "body_size_limit_middleware" in source, \
            "body_size_limit_middleware 必须通过 app.middleware 注册"

    @pytest.mark.asyncio
    async def test_large_content_length_rejected(self):
        """Content-Length 超过阈值 → 413"""
        from httpx import ASGITransport, AsyncClient

        # ASGI 模式测试 FastAPI app
        # 简化版：直接调用中间件函数
        from main import app, MAX_BODY_SIZE

        # 找一个会触发该中间件的路由
        # 构造一个超大的 content-length 请求
        from starlette.requests import Request as StarletteRequest
        from main import body_size_limit_middleware

        # 模拟 request
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/feishu/webhook",
            "headers": [(b"content-length", str(MAX_BODY_SIZE + 1).encode())],
            "query_string": b"",
        }
        request = StarletteRequest(scope)

        # 调用中间件
        async def dummy_call_next(req):
            from starlette.responses import JSONResponse
            return JSONResponse({"ok": True})

        response = await body_size_limit_middleware(request, dummy_call_next)
        assert response.status_code == 413
        body = json.loads(response.body)
        assert "too large" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_small_content_length_allowed(self):
        """Content-Length 在阈值内 → 放行"""
        from starlette.requests import Request as StarletteRequest
        from main import body_size_limit_middleware

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/feishu/webhook",
            "headers": [(b"content-length", b"1024")],
            "query_string": b"",
        }
        request = StarletteRequest(scope)

        async def dummy_call_next(req):
            from starlette.responses import JSONResponse
            return JSONResponse({"ok": True})

        response = await body_size_limit_middleware(request, dummy_call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_request_no_check(self):
        """GET 请求不受 body 限制"""
        from starlette.requests import Request as StarletteRequest
        from main import body_size_limit_middleware

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
        request = StarletteRequest(scope)

        called = False

        async def dummy_call_next(req):
            nonlocal called
            called = True
            from starlette.responses import JSONResponse
            return JSONResponse({"ok": True})

        response = await body_size_limit_middleware(request, dummy_call_next)
        assert called, "GET 请求应直接通过中间件"
        assert response.status_code == 200


# ===== P1 #15: Redis 去重 =====

class TestDedupCheck:
    """P1 #15: 用 Redis SETNX 做去重"""

    @pytest.mark.asyncio
    async def test_first_call_returns_true(self):
        """首次调用应返回 True"""
        from storage.state_manager import StateManager

        sm = StateManager()
        # Mock redis 客户端
        sm.redis = MagicMock()
        sm.redis.set = AsyncMock(return_value=True)  # SETNX 成功

        result = await sm.dedup_check("msg_123")
        assert result is True
        sm.redis.set.assert_called_once()
        # 验证调用参数
        call_args = sm.redis.set.call_args
        assert "dedup:msg_123" in call_args[0]
        assert call_args.kwargs.get("nx") is True
        assert call_args.kwargs.get("ex") == 86400  # 默认 24h

    @pytest.mark.asyncio
    async def test_duplicate_call_returns_false(self):
        """重复调用应返回 False"""
        from storage.state_manager import StateManager

        sm = StateManager()
        sm.redis = MagicMock()
        sm.redis.set = AsyncMock(return_value=False)  # SETNX 失败（已存在）

        result = await sm.dedup_check("msg_duplicate")
        assert result is False

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        """自定义 TTL 应传递给 Redis"""
        from storage.state_manager import StateManager

        sm = StateManager()
        sm.redis = MagicMock()
        sm.redis.set = AsyncMock(return_value=True)

        await sm.dedup_check("msg_x", ttl=3600)
        call_args = sm.redis.set.call_args
        assert call_args.kwargs.get("ex") == 3600

    @pytest.mark.asyncio
    async def test_key_prefix(self):
        """key 应加 dedup: 前缀"""
        from storage.state_manager import StateManager

        sm = StateManager()
        sm.redis = MagicMock()
        sm.redis.set = AsyncMock(return_value=True)

        await sm.dedup_check("raw_key")
        # 第一位置参数是 key
        call_args = sm.redis.set.call_args
        key_arg = call_args[0][0]
        assert key_arg == "dedup:raw_key"
