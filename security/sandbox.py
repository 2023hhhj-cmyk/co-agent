"""
SandboxedExecutor — Docker 沙箱执行器

限制 Claude Code 子进程的资源访问：
- Docker 容器隔离（优先）
- 降级模式：仅允许只读操作

资源限制：
| 资源   | 限制        |
|--------|------------|
| CPU    | 1 核       |
| 内存   | 512MB      |
| 网络   | 完全隔离    |
| 文件系统 | 只读根 + 项目目录可写 |
| 超时   | 300s       |
"""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import List, Optional

from observability.metrics import sandbox_mode as sandbox_metric

logger = logging.getLogger(__name__)

# 沙箱配置
DOCKER_IMAGE = "muzhu-sandbox:latest"
SANDBOX_TIMEOUT = 300  # 5 分钟
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1.0"

# 降级模式允许的工具（只读）
READONLY_TOOLS = ["Read", "Bash(read-only)", "Glob", "Grep"]
# 完整工具集
FULL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


@dataclass
class SandboxResult:
    """沙箱执行结果"""
    success: bool
    output: str = ""
    error: str = ""
    sandbox_mode: str = "docker"  # docker | fallback_readonly | native


class SandboxedExecutor:
    """Docker 沙箱执行器（带降级）"""

    def __init__(
        self,
        base_dir: str = "/root",
        docker_image: str = DOCKER_IMAGE,
        timeout: int = SANDBOX_TIMEOUT,
    ):
        self.base_dir = base_dir
        self.docker_image = docker_image
        self.timeout = timeout
        self._docker_available: Optional[bool] = None
        self._check_docker()

    def _check_docker(self) -> bool:
        """检查 Docker 是否可用"""
        if self._docker_available is not None:
            return self._docker_available

        docker_path = shutil.which("docker")
        if not docker_path:
            logger.warning("Docker 未安装，将使用降级只读模式")
            self._docker_available = False
            sandbox_metric.set(0)
            return False

        self._docker_available = True
        sandbox_metric.set(1)
        return True

    # 允许的工作目录前缀（白名单）
    _ALLOWED_PREFIXES = ["/root", "/tmp", "/workspace"]

    def _validate_working_dir(self, working_dir: Optional[str] = None) -> str:
        """验证工作目录合法性：解析符号链接、限制在白名单内"""
        cwd = working_dir or self.base_dir
        real_cwd = os.path.realpath(cwd)
        if not os.path.isdir(real_cwd):
            raise ValueError(f"[Security] 工作目录不存在: {real_cwd}")
        if not any(real_cwd.startswith(prefix) for prefix in self._ALLOWED_PREFIXES):
            raise ValueError(f"[Security] 工作目录不在允许范围内: {real_cwd}")
        return real_cwd

    @property
    def mode(self) -> str:
        """当前沙箱模式"""
        if self._check_docker():
            return "docker"
        return "fallback_readonly"

    @property
    def allowed_tools(self) -> List[str]:
        """当前模式允许的工具"""
        if self._check_docker():
            return FULL_TOOLS
        return READONLY_TOOLS

    async def execute(
        self,
        task: str,
        context: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        """
        在沙箱中执行 Claude Code 任务

        Args:
            task: 任务描述
            context: 附加上下文
            working_dir: 工作目录（挂载到容器的 /workspace）

        Returns:
            SandboxResult
        """
        if self._check_docker():
            return await self._execute_docker(task, context, working_dir)
        else:
            return await self._execute_fallback(task, context, working_dir)

    async def _execute_docker(
        self,
        task: str,
        context: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        """Docker 容器内执行"""
        cwd = self._validate_working_dir(working_dir)

        # 构建完整 prompt
        full_prompt = task
        if context:
            full_prompt = f"{context}\n\n---\n{task}"

        cmd = [
            "docker", "run", "--rm",
            "--memory", MEMORY_LIMIT,
            "--cpus", CPU_LIMIT,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,size=256m",
            "-v", f"{cwd}:/workspace:rw",
            "-w", "/workspace",
            self.docker_image,
            "claude", "-p", full_prompt,
        ]

        # 添加允许的工具
        for tool in FULL_TOOLS:
            cmd.extend(["--allowed-tools", tool])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return SandboxResult(
                    success=False,
                    error=f"沙箱执行超时 ({self.timeout}s)",
                    sandbox_mode="docker",
                )

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            error = stderr.decode("utf-8", errors="replace") if stderr else ""

            success = process.returncode == 0

            return SandboxResult(
                success=success,
                output=output,
                error=error,
                sandbox_mode="docker",
            )

        except FileNotFoundError:
            logger.error("Docker 命令执行失败，回退到降级模式")
            self._docker_available = False
            sandbox_metric.set(0)
            return await self._execute_fallback(task, context, working_dir)
        except Exception as e:
            logger.error(f"沙箱执行异常: {e}")
            return SandboxResult(
                success=False,
                error=str(e),
                sandbox_mode="docker",
            )

    async def _execute_fallback(
        self,
        task: str,
        context: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> SandboxResult:
        """
        降级模式：仅允许只读操作

        不使用 Docker，直接调用 claude 但限制工具为只读。
        """
        cwd = self._validate_working_dir(working_dir)
        logger.warning(f"使用降级只读模式执行: working_dir={cwd}")

        full_prompt = task
        if context:
            full_prompt = f"{context}\n\n---\n{task}"

        cmd = [
            "claude", "-p", full_prompt,
        ]
        for tool in READONLY_TOOLS:
            cmd.extend(["--allowed-tools", tool])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return SandboxResult(
                    success=False,
                    error=f"执行超时 ({self.timeout}s)",
                    sandbox_mode="fallback_readonly",
                )

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            error = stderr.decode("utf-8", errors="replace") if stderr else ""

            return SandboxResult(
                success=process.returncode == 0,
                output=output,
                error=error,
                sandbox_mode="fallback_readonly",
            )

        except Exception as e:
            logger.error(f"降级执行异常: {e}")
            return SandboxResult(
                success=False,
                error=str(e),
                sandbox_mode="fallback_readonly",
            )
