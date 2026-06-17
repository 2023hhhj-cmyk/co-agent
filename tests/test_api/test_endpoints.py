"""API 端点测试 (4 个用例，需要服务器运行)"""

import pytest
import httpx


BASE_URL = "http://localhost:8080"


class TestAPIEndpoints:
    """FastAPI 端点"""

    async def test_root(self):
        """GET / → 返回基本信息"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/")
            assert resp.status_code == 200
            data = resp.json()
            assert "Muzhu-AI" in data.get("message", "")

    async def test_health(self):
        """GET /health → 返回依赖状态"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/health")
            assert resp.status_code == 200
            data = resp.json()
            assert "status" in data
            assert "dependencies" in data
            assert "postgresql" in data["dependencies"]

    async def test_metrics(self):
        """GET /metrics → 返回 Prometheus 格式"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/metrics")
            assert resp.status_code == 200
            text = resp.text
            assert "muzhu_agent_state" in text
            assert "muzhu_task_duration_seconds" in text
            assert "muzhu_sandbox_mode" in text

    async def test_agents(self):
        """GET /api/agents → 返回 Agent 列表"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/api/agents")
            assert resp.status_code == 200
            data = resp.json()
            assert "agents" in data
