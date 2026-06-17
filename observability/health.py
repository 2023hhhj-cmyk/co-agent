"""
增强健康检查

检查所有依赖的连通性，不依赖虚假的 "always healthy" 响应。

检查项：
- PostgreSQL: SELECT 1
- Redis: PING
- ChromaDB: heartbeat API
- Docker: docker info

使用方式：
  checker = HealthChecker(state_manager, database)
  status = await checker.check()
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HealthChecker:
    """增强健康检查器"""

    def __init__(
        self,
        state_manager=None,
        database=None,
        chromadb_url: str = "",
        agents: Optional[Dict[str, Any]] = None,
    ):
        self.state_manager = state_manager
        self.database = database
        self.chromadb_url = chromadb_url
        self.agents = agents or {}
        self._start_time = datetime.now(timezone.utc)

    async def check(self) -> Dict[str, Any]:
        """执行全部健康检查"""
        deps = await asyncio.gather(
            self._check_postgresql(),
            self._check_redis(),
            self._check_chromadb(),
            self._check_docker(),
            return_exceptions=True,
        )

        dep_names = ["postgresql", "redis", "chromadb", "docker"]
        dependencies = {}
        for name, result in zip(dep_names, deps):
            if isinstance(result, Exception):
                dependencies[name] = "error"
            else:
                dependencies[name] = result

        # Agent 状态
        agent_states = {}
        for name, agent in self.agents.items():
            agent_states[name] = getattr(agent, "state", "unknown")

        # 总体状态
        all_ok = all(v == "ok" for v in dependencies.values())

        # 详细状态仅记日志，不暴露给客户端
        logger.info(f"Health check: {dependencies}, agents: {agent_states}")

        return {
            "status": "healthy" if all_ok else "degraded",
        }

    async def _check_postgresql(self) -> str:
        if not self.database:
            return "not_configured"
        try:
            await asyncio.wait_for(
                self.database.fetch("SELECT 1"), timeout=3.0
            )
            return "ok"
        except Exception as e:
            logger.warning(f"PostgreSQL health check failed: {e}")
            return f"error: {str(e)[:100]}"

    async def _check_redis(self) -> str:
        if not self.state_manager or not self.state_manager.redis:
            return "not_configured"
        try:
            result = await asyncio.wait_for(
                self.state_manager.redis.ping(), timeout=3.0
            )
            return "ok" if result else "no_response"
        except Exception as e:
            logger.warning(f"Redis health check failed: {e}")
            return f"error: {str(e)[:100]}"

    async def _check_chromadb(self) -> str:
        if not self.chromadb_url:
            return "not_configured"
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await asyncio.wait_for(
                    client.get(f"{self.chromadb_url}/api/v1/heartbeat"),
                    timeout=3.0,
                )
                return "ok" if resp.status_code == 200 else f"status_{resp.status_code}"
        except Exception as e:
            logger.warning(f"ChromaDB health check failed: {e}")
            return f"error: {str(e)[:100]}"

    async def _check_docker(self) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return "ok" if proc.returncode == 0 else "unavailable"
        except FileNotFoundError:
            return "not_installed"
        except Exception as e:
            return f"error: {str(e)[:100]}"
