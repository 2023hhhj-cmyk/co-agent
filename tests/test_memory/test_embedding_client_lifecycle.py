"""Embedding clients must not leak between independently closed event loops."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from config.settings import settings
from memory.knowledge import _embed_text


def test_embedding_client_is_closed_and_not_reused_across_event_loops(monkeypatch):
    monkeypatch.setattr(settings, "DASHSCOPE_API_KEY", "test-only")
    monkeypatch.setattr(settings, "DASHSCOPE_EMBEDDING_DIMENSIONS", 2)
    response = MagicMock()
    response.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
    clients = []

    def factory(**kwargs):
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        clients.append(client)
        return client

    monkeypatch.setattr("memory.knowledge.httpx.AsyncClient", factory)
    assert asyncio.run(_embed_text("first")) == [0.1, 0.2]
    assert asyncio.run(_embed_text("second")) == [0.1, 0.2]
    assert len(clients) == 2
    for client in clients:
        client.post.assert_awaited_once()
        client.__aexit__.assert_awaited_once()
