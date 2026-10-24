"""
Docker 工具封装 - 管理容器生命周期

核心功能：
- 镜像构建和管理
- 容器运行和停止
- 端口管理（PortManager）
- 日志收集
"""

import asyncio
import logging
import socket
import time
from typing import Optional

try:
    from analytics import emit_event, hash_args  # 埋点（agent 简历数据采集）
except Exception:  # noqa: BLE001
    def emit_event(*_a, **_kw):  # type: ignore[no-redef]
        return None
    def hash_args(_a):  # type: ignore[no-redef]
        return ""

from models.deploy import ServiceStatus

logger = logging.getLogger(__name__)


class PortManager:
    """
    端口管理器

    管理 8001-9000 范围内的端口分配。
    """

    def __init__(self, start: int = 8001, end: int = 9000, database=None):
        self.start = start
        self.end = end
        self.database = database
        self.allocated: dict[int, str] = {}  # port -> service_name
        self._next_port = start

    async def load_allocated(self):
        """从数据库灌回仍占用的端口。"""
        if not self.database:
            return
        rows = await self.database.fetch(
            "SELECT port, service_name FROM deployments "
            "WHERE port > 0 AND status <> 'stopped'"
        )
        for row in rows:
            self.allocated[int(row["port"])] = row["service_name"]
        if self.allocated:
            self._next_port = max(self.allocated) + 1
        logger.info("restored %s ports", len(self.allocated))

    async def allocate(self, service_name: str) -> int:
        """分配端口。先认领数据库里的空闲端口，再记到内存。"""
        for port, name in self.allocated.items():
            if name == service_name:
                logger.info("reuse port %s for %s", port, service_name)
                return port

        while self._next_port <= self.end:
            port = self._next_port
            self._next_port += 1
            if port in self.allocated or not self._is_port_available(port):
                continue
            if self.database:
                try:
                    await self.database.execute(
                        "INSERT INTO deployments "
                        "(service_name, kind, port, status, updated_at) "
                        "VALUES ($1, 'docker', $2, 'deploying', NOW()) "
                        "ON CONFLICT (service_name) DO UPDATE SET "
                        "port = EXCLUDED.port, kind = 'docker', "
                        "status = 'deploying', updated_at = NOW() "
                        "WHERE deployments.status = 'stopped' "
                        "OR deployments.port = 0 "
                        "OR deployments.port = EXCLUDED.port",
                        service_name, port,
                    )
                except Exception as exc:
                    logger.info("port %s claim failed: %s", port, exc)
                    continue
                row = await self.database.fetchrow(
                    "SELECT port FROM deployments "
                    "WHERE service_name = $1 AND port = $2 "
                    "AND status <> 'stopped'",
                    service_name, port,
                )
                if not row:
                    continue
            self.allocated[port] = service_name
            logger.info("allocated port %s for %s", port, service_name)
            return port

        raise RuntimeError(f"no free port in {self.start}-{self.end}")

    async def release(self, port: int):
        """释放端口，并把数据库记录标成已停止。"""
        service_name = self.allocated.pop(port, None)
        if service_name and self.database:
            await self.database.execute(
                "UPDATE deployments SET status = 'stopped', updated_at = NOW() "
                "WHERE service_name = $1 AND port = $2",
                service_name, port,
            )
        if service_name:
            logger.info("released port %s for %s", port, service_name)

    async def release_by_service(self, service_name: str):
        """释放服务占用的端口。"""
        ports = [port for port, name in self.allocated.items() if name == service_name]
        for port in ports:
            await self.release(port)

    def get_port(self, service_name: str) -> Optional[int]:
        """
        获取服务的端口

        Args:
            service_name: 服务名称

        Returns:
            Optional[int]: 端口号，如果未分配则返回 None
        """
        for port, name in self.allocated.items():
            if name == service_name:
                return port
        return None

    def get_all_allocated(self) -> dict[int, str]:
        """获取所有已分配的端口"""
        return self.allocated.copy()

    def _is_port_available(self, port: int) -> bool:
        """检查端口是否可用（未被系统占用）"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', port))
                return True
        except OSError:
            return False


class DockerManager:
    """
    Docker 管理器

    封装所有 Docker 操作。
    """

    def __init__(self, port_manager: Optional[PortManager] = None):
        self.port_manager = port_manager or PortManager()

    async def build(self, image_name: str, context_dir: str) -> bool:
        """
        构建 Docker 镜像

        Args:
            image_name: 镜像名称
            context_dir: 构建上下文目录

        Returns:
            bool: 是否成功
        """
        _args_hash = hash_args({"image": image_name, "dir": context_dir})
        emit_event("tool.call", tool_name="docker.build", phase="start", tool_args_hash=_args_hash)
        _start = time.time()
        try:
            logger.info(f"构建镜像: {image_name}，目录: {context_dir}")

            cmd = ["docker", "build", "-t", image_name, context_dir]
            result = await self._run_command(cmd)

            _ok = result.returncode == 0
            emit_event("tool.call", tool_name="docker.build", phase="end",
                       exit_code=result.returncode,
                       stdout_len=len(result.stdout or ""),
                       duration_ms=int((time.time() - _start) * 1000),
                       success=_ok)

            if _ok:
                logger.info(f"镜像构建成功: {image_name}")
                return True
            else:
                logger.error(f"镜像构建失败: {result.stderr}")
                return False
        except Exception:
            emit_event("tool.call", tool_name="docker.build", phase="end",
                       duration_ms=int((time.time() - _start) * 1000), success=False)
            raise

    async def run(
        self,
        container_name: str,
        image_name: str,
        port: int,
        internal_port: int = 8000,
        env: Optional[dict[str, str]] = None,
    ) -> bool:
        """
        运行 Docker 容器

        Args:
            container_name: 容器名称
            image_name: 镜像名称
            port: 宿主机端口
            internal_port: 容器内部端口
            env: 环境变量

        Returns:
            bool: 是否成功
        """
        _args_hash = hash_args({"name": container_name, "image": image_name, "port": port})
        emit_event("tool.call", tool_name="docker.run", phase="start", tool_args_hash=_args_hash)
        _start = time.time()
        try:
            logger.info(f"运行容器: {container_name}，端口: {port}->{internal_port}")

            # 先停止同名容器（如果存在）
            await self.stop(container_name)
            await self.remove(container_name)

            cmd = [
                "docker", "run", "-d",
                "--name", container_name,
                "--restart", "unless-stopped",
                "-p", f"{port}:{internal_port}",
            ]

            # 添加环境变量
            if env:
                for key, value in env.items():
                    cmd.extend(["-e", f"{key}={value}"])

            cmd.append(image_name)

            result = await self._run_command(cmd)

            _ok = result.returncode == 0
            emit_event("tool.call", tool_name="docker.run", phase="end",
                       exit_code=result.returncode,
                       stdout_len=len(result.stdout or ""),
                       duration_ms=int((time.time() - _start) * 1000),
                       success=_ok)

            if _ok:
                logger.info(f"容器运行成功: {container_name}")
                return True
            else:
                logger.error(f"容器运行失败: {result.stderr}")
                return False
        except Exception:
            emit_event("tool.call", tool_name="docker.run", phase="end",
                       duration_ms=int((time.time() - _start) * 1000), success=False)
            raise

    async def stop(self, container_name: str) -> bool:
        """
        停止容器

        Args:
            container_name: 容器名称

        Returns:
            bool: 是否成功
        """
        logger.info(f"停止容器: {container_name}")

        cmd = ["docker", "stop", container_name]
        result = await self._run_command(cmd)

        return result.returncode == 0

    async def remove(self, container_name: str) -> bool:
        """
        删除容器

        Args:
            container_name: 容器名称

        Returns:
            bool: 是否成功
        """
        logger.info(f"删除容器: {container_name}")

        cmd = ["docker", "rm", "-f", container_name]
        result = await self._run_command(cmd)

        return result.returncode == 0

    async def restart(self, container_name: str) -> bool:
        """
        重启容器

        Args:
            container_name: 容器名称

        Returns:
            bool: 是否成功
        """
        logger.info(f"重启容器: {container_name}")

        cmd = ["docker", "restart", container_name]
        result = await self._run_command(cmd)

        if result.returncode == 0:
            logger.info(f"容器重启成功: {container_name}")
            return True
        else:
            logger.error(f"容器重启失败: {result.stderr}")
            return False

    async def inspect(self, container_name: str) -> Optional[dict]:
        """
        获取容器信息

        Args:
            container_name: 容器名称

        Returns:
            Optional[dict]: 容器信息，如果容器不存在则返回 None
        """
        cmd = ["docker", "inspect", "--format", "{{json .}}", container_name]
        result = await self._run_command(cmd)

        if result.returncode == 0:
            import json
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return None
        return None

    async def is_running(self, container_name: str) -> bool:
        """
        检查容器是否在运行

        Args:
            container_name: 容器名称

        Returns:
            bool: 是否在运行
        """
        info = await self.inspect(container_name)
        if info:
            state = info.get("State", {})
            return state.get("Running", False)
        return False

    async def logs(self, container_name: str, tail: int = 100) -> str:
        """
        获取容器日志

        Args:
            container_name: 容器名称
            tail: 返回最后多少行

        Returns:
            str: 日志内容
        """
        cmd = ["docker", "logs", "--tail", str(tail), container_name]
        result = await self._run_command(cmd)

        return result.stdout or ""

    async def stats(self, container_name: str) -> Optional[dict]:
        """
        获取容器资源使用情况

        Args:
            container_name: 容器名称

        Returns:
            Optional[dict]: 资源使用情况
        """
        cmd = [
            "docker", "stats", "--no-stream", "--format",
            '{"cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","net":"{{.NetIO}}"}',
            container_name,
        ]
        result = await self._run_command(cmd)

        if result.returncode == 0:
            import json
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return None
        return None

    async def cleanup(self, days: int = 7):
        """
        清理旧的镜像和容器

        Args:
            days: 保留最近多少天
        """
        logger.info(f"清理 {days} 天前的镜像和容器")

        # 清理停止的容器
        cmd = ["docker", "container", "prune", "-f", "--filter", f"until={days}d"]
        await self._run_command(cmd)

        # 清理未使用的镜像
        cmd = ["docker", "image", "prune", "-f", "--filter", f"until={days}d"]
        await self._run_command(cmd)

        logger.info("清理完成")

    async def _run_command(self, cmd: list[str]) -> asyncio.subprocess.Process:
        """执行命令"""
        logger.debug(f"执行命令: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        return type('Result', (), {
            'returncode': process.returncode,
            'stdout': stdout.decode('utf-8', errors='replace').strip(),
            'stderr': stderr.decode('utf-8', errors='replace').strip(),
        })()
