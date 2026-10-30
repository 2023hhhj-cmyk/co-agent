"""Retry behavior through the real shared LLM entry point, without API calls."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from config.settings import settings
from tools.llm.client import ClaudeClient, DeepSeekClient
from tools.llm.rate_limit import LLMRateLimiter


@pytest.fixture
def retry_setup(monkeypatch):
    limiter = LLMRateLimiter(rpm=100, tpm=100000)
    monkeypatch.setattr("tools.llm.client.get_llm_rate_limiter", lambda: limiter)
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(settings, "LLM_RETRY_BASE_DELAY", 1.0)
    monkeypatch.setattr(settings, "LLM_RETRY_MAX_DELAY", 30.0)
    monkeypatch.setattr("tools.llm.client.random.uniform", lambda low, high: 0)
    sleeping = AsyncMock()
    monkeypatch.setattr("tools.llm.client.sleep", sleeping)
    return limiter, sleeping


def success_response(client):
    if isinstance(client, ClaudeClient):
        return httpx.Response(200, json={
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "content": [{"text": "answer"}],
        })
    return httpx.Response(200, json={
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        "choices": [{"message": {"content": "answer"}}],
    })


@pytest.mark.parametrize("client_type", [DeepSeekClient, ClaudeClient])
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 529])
async def test_transient_http_retries_both_providers_and_charges_every_attempt(retry_setup,
                                                                           client_type, status):
    limiter, sleeping = retry_setup
    client = client_type(api_key="test", model="test")
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(status) if len(attempts) < 3 else success_response(client)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    cache = MagicMock(get=AsyncMock(return_value=None), set=AsyncMock())
    client.set_cache(cache)
    try:
        assert await client.chat([{"role": "user", "content": "hello"}],
                                 temperature=0.3, max_tokens=100) == "answer"
        assert len(attempts) == 3
        assert [c.args[0] for c in sleeping.await_args_list] == [1, 2]
        assert len(limiter._requests) == 3
        assert all(not r.pending for r in limiter._tokens)
        assert limiter._tokens[0].tokens > 100
        assert limiter._tokens[1].tokens > 100
        assert limiter._tokens[2].tokens == 12
        cache.set.assert_awaited_once()
        assert cache.set.await_args.args[-1] is True
    finally:
        await client.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_http_errors_not_retried(retry_setup, status):
    limiter, sleeping = retry_setup
    client = DeepSeekClient(api_key="test")
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(status)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await client.chat([{"role": "user", "content": "hello"}])
        assert caught.value.response.status_code == status
        assert len(attempts) == len(limiter._requests) == 1
        sleeping.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError,
                                        httpx.RemoteProtocolError])
async def test_network_errors_stop_at_retry_limit_and_preserve_exception(retry_setup,
                                                                      monkeypatch, error_type):
    limiter, sleeping = retry_setup
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 5)
    monkeypatch.setattr(settings, "LLM_RETRY_MAX_DELAY", 5)
    client = DeepSeekClient(api_key="test")
    original = error_type("transient")
    client._chat_impl = AsyncMock(side_effect=original)
    with pytest.raises(error_type) as caught:
        await client.chat([{"role": "user", "content": "hello"}], max_tokens=100)
    assert caught.value is original
    assert client._chat_impl.await_count == 6
    assert [c.args[0] for c in sleeping.await_args_list] == [1, 2, 4, 5, 5]
    assert len(limiter._requests) == 6
    assert all(not r.pending for r in limiter._tokens)


@pytest.mark.parametrize("header", ["8", "date", "invalid", "NaN", "inf"])
async def test_retry_after_seconds_and_http_date(retry_setup, monkeypatch, header):
    _, sleeping = retry_setup
    monkeypatch.setattr(settings, "LLM_RETRY_MAX_DELAY", 2)
    if header == "date":
        header_value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=8), usegmt=True)
    else:
        header_value = header
    client = DeepSeekClient(api_key="test")
    attempts = []

    def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": header_value})
        return success_response(client)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await client.chat([{"role": "user", "content": "hello"}]) == "answer"
        delay = sleeping.await_args.args[0]
        if header == "8":
            assert delay == 8
        elif header == "date":
            assert 6 <= delay <= 8
        else:
            assert delay == 1
    finally:
        await client.close()


async def test_cancellation_during_backoff_does_not_retry(retry_setup, monkeypatch):
    limiter, _ = retry_setup
    entered = asyncio.Event()

    async def blocked_sleep(delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("tools.llm.client.sleep", blocked_sleep)
    client = DeepSeekClient(api_key="test")
    client._chat_impl = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    job = asyncio.create_task(client.chat([{"role": "user", "content": "hello"}]))
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    client._chat_impl.assert_awaited_once()
    assert len(limiter._requests) == 1
    assert not limiter._tokens[0].pending


async def test_retries_still_wait_for_rpm_quota(retry_setup, monkeypatch):
    limiter = LLMRateLimiter(rpm=1, tpm=100000, window_seconds=0.04)
    monkeypatch.setattr("tools.llm.client.get_llm_rate_limiter", lambda: limiter)
    client = DeepSeekClient(api_key="test")
    timestamps = []

    def handle(request):
        timestamps.append(asyncio.get_running_loop().time())
        return httpx.Response(503) if len(timestamps) == 1 else success_response(client)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await asyncio.wait_for(client.chat([{"role": "user", "content": "hello"}]), 1) == "answer"
        assert timestamps[1] - timestamps[0] >= 0.035
    finally:
        await client.close()


async def test_zero_retries_disables_retry(retry_setup, monkeypatch):
    _, sleeping = retry_setup
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 0)
    client = DeepSeekClient(api_key="test")
    client._chat_impl = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    with pytest.raises(httpx.ReadTimeout):
        await client.chat([{"role": "user", "content": "hello"}])
    client._chat_impl.assert_awaited_once()
    sleeping.assert_not_awaited()


async def test_jitter_is_applied_to_retry_delay(retry_setup, monkeypatch):
    _, sleeping = retry_setup
    monkeypatch.setattr("tools.llm.client.random.uniform", lambda low, high: high)
    client = DeepSeekClient(api_key="test")
    client._chat_impl = AsyncMock(side_effect=[httpx.ReadTimeout("timeout"), "answer"])
    assert await client.chat([{"role": "user", "content": "hello"}]) == "answer"
    assert sleeping.await_args.args[0] == 1.25


def test_cache_key_uses_full_messages_and_all_history():
    from tools.llm.client import DeepSeekClient
    from acceleration.cache import ResponseCache
    client=DeepSeekClient(api_key="test",model="test")
    prefix="a"*200
    first=[{"role":"user","content":prefix+"first"}]
    second=[{"role":"user","content":prefix+"second"}]
    first_prompt=client._build_prompt_string(first)
    second_prompt=client._build_prompt_string(second)
    assert first_prompt != second_prompt
    cache=ResponseCache()
    assert cache._cache_key(first_prompt,"test",0.3) != cache._cache_key(second_prompt,"test",0.3)
    history=[{"role":"user","content":"old-first"}]+[{"role":"user","content":str(i)} for i in range(10)]
    changed=[{"role":"user","content":"old-second"}]+history[1:]
    assert client._build_prompt_string(history) != client._build_prompt_string(changed)
