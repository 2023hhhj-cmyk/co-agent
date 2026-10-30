"""Behavior checks for workspace job admission and shared model quotas."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from config.settings import Settings, settings
from communication.queue import QueueWorker
from models.message import Message
from models.task import ProjectPlan, Task
from tools.llm.client import DeepSeekClient
from tools.llm.rate_limit import LLMRateLimiter, get_llm_rate_limiter
from workflows.converter import PlanToGraphConverter
from workflows.executor import WorkflowExecutor
from workflows.graph import WorkflowGraph


def workflow(handler, count=4):
    plan = ProjectPlan(requirement="concurrency", tasks=[
        Task(title=f"task-{i}", description="run", assigned_to=f"role-{i}")
        for i in range(count)
    ])
    graph = WorkflowGraph(PlanToGraphConverter().convert(plan))
    agents = {t.assigned_to: SimpleNamespace(run=handler) for t in plan.tasks}
    return WorkflowExecutor(graph, agents)


@pytest.mark.parametrize("global_limit, local_limit, expected_peak", [(2, 8, 2), (5, 1, 2)])
async def test_workflows_share_global_slots_and_obey_local_cap(monkeypatch, global_limit,
                                                            local_limit, expected_peak):
    monkeypatch.setattr(settings, "WORKSPACE_MAX_CONCURRENT_TASKS", global_limit)
    active = peak = 0

    async def handler(message):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return Message(content="done")
        finally:
            active -= 1

    results = await asyncio.gather(*[
        workflow(handler).execute(Message(content="start"), max_parallel=local_limit)
        for _ in range(2)
    ])
    assert peak == expected_peak
    assert active == 0
    assert all(len(r["outputs"]) == 4 for r in results)


async def test_customer_worker_and_workflow_share_slots(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_MAX_CONCURRENT_TASKS", 1)
    active = peak = 0
    calls = []

    async def handler(message):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(message)
        try:
            await asyncio.sleep(0.005)
            return Message(content="done")
        finally:
            active -= 1

    worker = QueueWorker(MagicMock(), handler)
    await asyncio.gather(workflow(handler).execute(Message(content="start")),
                         worker._safe_handle(MagicMock(customer_id="customer")))
    assert peak == 1
    assert len(calls) == 5


async def test_cancellation_and_failure_release_workspace_slots(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_MAX_CONCURRENT_TASKS", 1)
    started = asyncio.Event()

    async def blocked(message):
        started.set()
        await asyncio.Event().wait()

    job = asyncio.create_task(workflow(blocked, 1).execute(Message(content="start")))
    await started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    failing = AsyncMock(side_effect=RuntimeError("failed"))
    result = await asyncio.wait_for(workflow(failing, 1).execute(Message(content="start")), 1)
    assert "error" in next(iter(result["outputs"].values()))
    result = await asyncio.wait_for(
        workflow(AsyncMock(return_value=Message(content="ok")), 1).execute(Message(content="start")), 1)
    assert next(iter(result["outputs"].values()))["content"] == "ok"


async def test_rpm_counts_all_concurrent_admissions_in_rolling_window():
    limiter = LLMRateLimiter(rpm=2, tpm=1000, window_seconds=0.04)
    admitted = []

    async def request():
        reservation = await limiter.acquire(1)
        admitted.append(asyncio.get_running_loop().time())
        await limiter.finish(reservation, 1)

    await asyncio.wait_for(asyncio.gather(*(request() for _ in range(5))), 1)
    assert admitted[2] - admitted[0] >= 0.04
    assert admitted[4] - admitted[2] >= 0.04


async def test_tpm_expiry_and_actual_usage_reconciliation():
    limiter = LLMRateLimiter(rpm=100, tpm=10, window_seconds=0.04)
    reservation = await limiter.acquire(8)
    waiting = asyncio.create_task(limiter.acquire(6))
    await asyncio.sleep(0)
    assert not waiting.done()
    await limiter.finish(reservation, 2)
    admitted = await asyncio.wait_for(waiting, 1)
    await limiter.finish(admitted, 6)
    before = asyncio.get_running_loop().time()
    final = await asyncio.wait_for(limiter.acquire(6), 1)
    assert asyncio.get_running_loop().time() - before >= 0.035
    await limiter.finish(final, 6)


async def test_pending_tokens_do_not_expire_and_waiting_cancellation_is_safe():
    limiter = LLMRateLimiter(rpm=100, tpm=10, window_seconds=0.02)
    reservation = await limiter.acquire(8)
    waiting = asyncio.create_task(limiter.acquire(6))
    await asyncio.sleep(0.04)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await limiter.finish(reservation, 0)
    next_request = await asyncio.wait_for(limiter.acquire(10), 1)
    await limiter.finish(next_request, 10)


async def test_oversized_request_rejected_without_consuming_budget():
    limiter = LLMRateLimiter(rpm=1, tpm=10)
    with pytest.raises(ValueError, match="LLM_TPM_LIMIT"):
        await limiter.acquire(11)
    reservation = await limiter.acquire(10)
    await limiter.finish(reservation, 10)


async def test_distinct_llm_clients_share_quota_and_cache_hits_bypass_it(monkeypatch):
    limiter = LLMRateLimiter(rpm=1, tpm=10000, window_seconds=0.04)
    monkeypatch.setattr("tools.llm.client.get_llm_rate_limiter", lambda: limiter)
    calls = []

    async def handle(request):
        calls.append(asyncio.get_running_loop().time())
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            "choices": [{"message": {"content": "answer"}}],
        })

    clients = [DeepSeekClient(api_key="test", base_url="https://example.test", model="test")
               for _ in range(2)]
    for client in clients:
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await asyncio.gather(*(c.chat([{"role": "user", "content": "hello"}],
                                             max_tokens=100) for c in clients)) == ["answer"] * 2
        assert calls[1] - calls[0] >= 0.04
        assert limiter._tokens and all(r.tokens == 12 for r in limiter._tokens)
        cache = MagicMock(get=AsyncMock(return_value="cached"))
        clients[0].set_cache(cache)
        assert await asyncio.wait_for(clients[0].chat(
            [{"role": "user", "content": "hello"}], temperature=0.3), 0.02) == "cached"
        assert len(calls) == 2
    finally:
        await asyncio.gather(*(c.close() for c in clients))


async def test_llm_cancellation_keeps_unknown_usage_reservation(monkeypatch):
    limiter = LLMRateLimiter(rpm=100, tpm=10000)
    monkeypatch.setattr("tools.llm.client.get_llm_rate_limiter", lambda: limiter)
    client = DeepSeekClient(api_key="test")
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    client._chat_impl = blocked
    job = asyncio.create_task(client.chat([{"role": "user", "content": "hello"}], max_tokens=100))
    await entered.wait()
    reserved = limiter._tokens[0].tokens
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert not limiter._tokens[0].pending
    assert limiter._tokens[0].tokens == reserved


async def test_default_llm_limiter_is_shared():
    assert get_llm_rate_limiter() is get_llm_rate_limiter()


@pytest.mark.parametrize("field", ["WORKSPACE_MAX_CONCURRENT_TASKS", "LLM_RPM_LIMIT", "LLM_TPM_LIMIT"])
def test_nonpositive_limits_rejected(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


async def test_failed_llm_request_retains_budget_and_preserves_error(monkeypatch):
    limiter = LLMRateLimiter(rpm=100, tpm=10000)
    monkeypatch.setattr("tools.llm.client.get_llm_rate_limiter", lambda: limiter)
    client = DeepSeekClient(api_key="test")
    original = RuntimeError("provider rejected request")
    client._chat_impl = AsyncMock(side_effect=original)
    with pytest.raises(RuntimeError) as caught:
        await client.chat([{"role": "user", "content": "hello"}], max_tokens=100)
    assert caught.value is original
    assert len(limiter._requests) == 1
    assert limiter._tokens[0].tokens > 100
    assert not limiter._tokens[0].pending


async def test_oversized_llm_prompt_never_calls_provider(monkeypatch):
    limiter = LLMRateLimiter(rpm=100, tpm=100)
    monkeypatch.setattr("tools.llm.client.get_llm_rate_limiter", lambda: limiter)
    client = DeepSeekClient(api_key="test")
    client._chat_impl = AsyncMock()
    with pytest.raises(ValueError, match="LLM_TPM_LIMIT"):
        await client.chat([{"role": "user", "content": "x" * 1000}], max_tokens=10)
    client._chat_impl.assert_not_awaited()
