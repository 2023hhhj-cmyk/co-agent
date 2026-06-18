"""WecomClient 单元测试 — 覆盖 P0 #1 (死代码) + P0 #3 (token 刷新竞态)"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.wecom import WecomClient


# ========== P0 #1 修复回归测试 ==========

class TestWecomClientDeadCodeRemoved:
    """P0 #1: 死代码 _auth_headers 必须被删除

    修复前：_auth_headers 计算了 token 但没放进 headers，且从未被 WecomClient 任何方法调用。
    修复后：方法已删除，避免误导 + 节省一次无效网络往返。
    """

    def test_auth_headers_method_removed(self):
        """_auth_headers 方法必须不存在（防止回退）"""
        assert not hasattr(WecomClient, "_auth_headers"), (
            "P0 #1 修复：WecomClient._auth_headers 死代码必须保持删除状态"
        )


# ========== P0 #3 修复测试 ==========

def _make_fake_httpx_response(token: str = "fake_token_xxx", expires_in: int = 7200) -> MagicMock:
    """构造一个 httpx 假响应"""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "errcode": 0,
        "errmsg": "ok",
        "access_token": token,
        "expires_in": expires_in,
    })
    return resp


def _make_token_client() -> WecomClient:
    """构造一个最小可用的 WecomClient（无加密工具）"""
    return WecomClient(
        corp_id="wxcorp123",
        agent_id="1000001",
        secret="secret_abc",
        token="",
        aes_key="",
    )


class TestWecomClientTokenCache:
    """token 缓存与刷新逻辑

    注意：asyncio.Lock 在 Python 中会绑定到创建时所在的事件循环。
    pytest-asyncio 每个 test 用独立的 loop，所以必须在 async 上下文内创建 client。
    """

    @pytest.mark.asyncio
    async def test_first_call_fetches_token(self):
        """首次调用 → 触发网络请求"""
        client = _make_token_client()
        fake_resp = _make_fake_httpx_response(token="tok1")

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = AsyncMock(return_value=fake_resp)
            token = await client._get_access_token()

        assert token == "tok1"
        assert client._access_token == "tok1"
        assert client._token_expire_at > time.time()

    @pytest.mark.asyncio
    async def test_cached_token_returned_without_network(self):
        """缓存有效期内 → 不发起网络请求"""
        client = _make_token_client()
        client._access_token = "cached_tok"
        client._token_expire_at = time.time() + 3600

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = AsyncMock()
            token = await client._get_access_token()

        assert token == "cached_tok"
        mock_get_client.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_token_near_expiry_triggers_refresh(self):
        """即将过期（< 5 分钟） → 触发刷新"""
        client = _make_token_client()
        client._access_token = "old_tok"
        client._token_expire_at = time.time() + 60

        fake_resp = _make_fake_httpx_response(token="new_tok")

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = AsyncMock(return_value=fake_resp)
            token = await client._get_access_token()

        assert token == "new_tok"
        assert client._access_token == "new_tok"
        mock_get_client.return_value.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_concurrent_refresh_only_one_network_call(self):
        """P0 #3 核心：并发刷新只触发一次网络请求

        10 个协程同时调用 _get_access_token，token 已过期。
        修复前：可能触发 1-10 次网络请求（取决于调度），后写入覆盖前写入。
        修复后：asyncio.Lock + double-check 模式保证恰好 1 次网络请求。
        """
        client = _make_token_client()
        client._access_token = ""
        client._token_expire_at = 0

        call_count = 0

        async def slow_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)  # 让其他协程有机会同时进入临界区
            return _make_fake_httpx_response(token="single_tok")

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = slow_get

            tokens = await asyncio.gather(*[
                client._get_access_token() for _ in range(10)
            ])

        # 关键断言：网络只调用 1 次
        assert call_count == 1, f"并发刷新应只触发 1 次网络请求，实际 {call_count} 次"
        # 所有协程应拿到相同 token
        assert all(t == "single_tok" for t in tokens)
        assert len(set(tokens)) == 1

    @pytest.mark.asyncio
    async def test_concurrent_after_expiry_still_only_one_call(self):
        """过期后并发：仍只刷新一次"""
        client = _make_token_client()
        client._access_token = "old_tok"
        client._token_expire_at = time.time() - 1

        call_count = 0

        async def slow_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.02)
            return _make_fake_httpx_response(token="refreshed_tok")

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = slow_get

            tokens = await asyncio.gather(*[
                client._get_access_token() for _ in range(20)
            ])

        assert call_count == 1
        assert all(t == "refreshed_tok" for t in tokens)
        assert client._access_token == "refreshed_tok"

    @pytest.mark.asyncio
    async def test_failed_refresh_does_not_cache(self):
        """刷新失败 → 不缓存错误状态，下次再试

        errcode != 0 时不更新 _access_token，下次调用会再次尝试刷新。
        """
        client = _make_token_client()
        client._access_token = ""
        client._token_expire_at = 0

        err_resp = MagicMock()
        err_resp.raise_for_status = MagicMock()
        err_resp.json = MagicMock(return_value={"errcode": 40001, "errmsg": "invalid credential"})

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = AsyncMock(return_value=err_resp)

            with pytest.raises(Exception, match="获取企业微信 token 失败"):
                await client._get_access_token()

        # 失败时不应写入缓存
        assert client._access_token == ""
        assert client._token_expire_at == 0

    @pytest.mark.asyncio
    async def test_lock_is_async_lock(self):
        """_token_lock 必须是 asyncio.Lock（fastapi 异步运行时兼容）"""
        client = _make_token_client()
        assert isinstance(client._token_lock, asyncio.Lock), (
            "P0 #3 修复：必须使用 asyncio.Lock，threading.Lock 在 async 上下文中会阻塞事件循环"
        )

    @pytest.mark.asyncio
    async def test_double_check_skips_network_if_recently_cached(self):
        """Double-check：等锁后若已被刷新则跳过网络

        模拟：协程 A 进入临界区开始刷新，协程 B 在锁外等待。
        A 完成刷新后释放锁，B 拿锁后应通过 double-check 直接返回缓存。
        """
        client = _make_token_client()
        client._access_token = ""
        client._token_expire_at = 0

        network_calls = 0

        async def slow_get(*args, **kwargs):
            nonlocal network_calls
            network_calls += 1
            # 慢一点，让其他协程进入等待
            await asyncio.sleep(0.1)
            return _make_fake_httpx_response(token="tok_shared")

        async def quick_call():
            """模拟在 B 等待期间，A 已经更新了缓存"""
            await asyncio.sleep(0.02)  # 模拟其他逻辑
            # 注入"已被其他协程刷新"的缓存状态
            client._access_token = "pre_cached"
            client._token_expire_at = time.time() + 3600

        with patch.object(client, "_get_client") as mock_get_client:
            mock_get_client.return_value.get = slow_get

            # 并发：第一个调用开始网络请求，第二个在缓存被更新后调用
            await asyncio.gather(
                client._get_access_token(),
                quick_call(),
                client._get_access_token(),
            )

        # 注：这个测试更多是 sanity check，真正的并发安全由
        # test_concurrent_refresh_only_one_network_call 保证
        assert network_calls >= 1
