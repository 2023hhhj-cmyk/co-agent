"""API contract checks using an isolated in-process application."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import main
from config.settings import settings
from observability.health import HealthChecker


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "suite-only-key")
    checker = HealthChecker()
    for name in ("_check_postgresql", "_check_redis", "_check_pgvector", "_check_docker"):
        monkeypatch.setattr(checker, name, AsyncMock(return_value="ok"))
    monkeypatch.setattr(main, "health_checker", checker)
    monkeypatch.setattr(main, "agents", {"developer": SimpleNamespace(to_dict=lambda: {"name": "developer"})})
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as session:
        yield session


class TestAPIEndpoints:
    async def test_root(self, client):
        response = await client.get("/")
        assert response.status_code == 200
        assert "Muzhu-AI" in response.json()["message"]

    async def test_health(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
        for name in ("_check_postgresql", "_check_redis", "_check_pgvector", "_check_docker"):
            getattr(main.health_checker, name).assert_awaited_once()

    async def test_health_degraded_without_exposing_dependency_details(self, client):
        main.health_checker._check_redis.return_value = "error: internal details"
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "degraded"}

    async def test_metrics(self, client):
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert all(name in response.text for name in ("muzhu_agent_state", "muzhu_task_duration_seconds", "muzhu_sandbox_mode"))

    async def test_agents(self, client):
        assert (await client.get("/api/agents")).status_code == 401
        assert (await client.get("/api/agents", headers={"X-API-Key": "wrong"})).status_code == 401
        response = await client.get("/api/agents", headers={"X-API-Key": "suite-only-key"})
        assert response.status_code == 200
        assert response.json()["agents"]["developer"]["name"] == "developer"

    async def test_protected_endpoint_fails_closed_without_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_KEY", "")
        response = await client.get("/api/agents")
        assert response.status_code == 503
