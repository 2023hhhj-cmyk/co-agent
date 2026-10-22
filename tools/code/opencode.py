"""
opencode CLI 工具封装

通过 Docker 容器中的子进程调用 opencode CLI 执行编码任务。
模型凭证（OPENCODE_API_KEY）从白名单环境变量、Settings 或宿主机 opencode
登录态读取，不挂载宿主机个人目录。
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from security.sandbox import SandboxedExecutor
from analytics import emit_event, hash_args

logger = logging.getLogger(__name__)


@dataclass
class OpenCodeResult:
    """opencode 执行结果"""
    success: bool
    output: str
    error: Optional[str] = None
    working_dir: Optional[str] = None
    tokens: dict = None  # opencode token 用量（input/output/cache/cost）


class OpenCodeTool:
    """
    opencode CLI 工具封装

    通过子进程调用 opencode 命令行执行编码任务。
    使用前请确保：
    1. 已构建包含 opencode 的沙箱镜像（security/Dockerfile.opencode）
    2. 已配置 OPENCODE_API_KEY 与 OPENCODE_MODEL
    """

    def __init__(
        self,
        base_dir: str = "/root",
        timeout: int = 600,
        allowed_tools: Optional[list[str]] = None,
        sandbox: Any = None,
    ):
        """
        初始化 OpenCodeTool

        Args:
            base_dir: 基础目录，客户项目将创建在此目录下
            timeout: 命令超时时间（秒），默认 600 秒（10 分钟）
            allowed_tools: 允许使用的工具列表，None 表示使用默认工具
            sandbox: 可注入的沙箱执行器；默认强制使用 Docker，无宿主机降级
        """
        self.base_dir = base_dir
        self.timeout = timeout
        self.sandbox = sandbox if sandbox is not None else SandboxedExecutor(
            base_dir=base_dir, timeout=timeout, allowed_tools=allowed_tools,
        )
        self.allowed_tools = allowed_tools or [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
        ]

    def create_client_directory(self, client_name: str) -> str:
        """
        为客户创建专属目录

        Args:
            client_name: 客户名称或标识

        Returns:
            str: 创建的目录路径
        """
        # 清理客户名称，移除特殊字符
        safe_name = self._sanitize_name(client_name)
        client_dir = os.path.join(self.base_dir, safe_name)
        os.makedirs(client_dir, exist_ok=True)
        logger.info(f"创建客户目录: {client_dir}")
        return client_dir

    async def execute(
        self,
        task: str,
        working_dir: Optional[str] = None,
        context: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> OpenCodeResult:
        """
        执行编码任务

        Args:
            task: 任务描述
            working_dir: 工作目录。使用客户卷时可以为空
            context: 额外上下文信息
            volume: 客户数据卷。设置后挂载该卷，不再绑定宿主机目录
        """
        cwd = working_dir or (None if volume else self.base_dir)
        try:
            result = await self.sandbox.execute(task, context, cwd, volume=volume)
            tokens = result.tokens or {}
            self._emit_tool_call_event(task, result.success, tokens)
            return OpenCodeResult(
                success=result.success, output=result.output, error=result.error or None,
                working_dir=cwd, tokens=tokens,
            )
        except (OSError, ValueError) as exc:
            return OpenCodeResult(False, "", error=str(exc), working_dir=cwd)

    async def execute_multiple(
        self,
        tasks: list[dict],
        max_concurrent: int = 8,
    ) -> list[OpenCodeResult]:
        """
        并行执行多个任务

        Args:
            tasks: 任务列表，每个任务是字典，包含 task、working_dir、context
            max_concurrent: 最大并发数

        Returns:
            list[OpenCodeResult]: 执行结果列表
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def run_with_semaphore(task_info: dict) -> OpenCodeResult:
            async with semaphore:
                return await self.execute(
                    task=task_info["task"],
                    working_dir=task_info.get("working_dir"),
                    context=task_info.get("context"),
                )

        logger.info(f"并行执行 {len(tasks)} 个任务，最大并发数: {max_concurrent}")
        results = await asyncio.gather(
            *[run_with_semaphore(task) for task in tasks],
            return_exceptions=True,
        )

        # 处理异常情况
        final_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                final_results.append(
                    OpenCodeResult(
                        success=False,
                        output="",
                        error=f"任务执行异常: {str(result)}",
                        working_dir=tasks[i].get("working_dir"),
                    )
                )
            else:
                final_results.append(result)

        return final_results

    def _emit_tool_call_event(self, task: str, success: bool, tokens: dict) -> None:
        """统一工具调用埋点（含 token 用量）。所有 opencode.execute() 调用都自动埋点。"""
        try:
            emit_event(
                "tool.call",
                tool_name="opencode",
                phase="end",
                tool_args_hash=hash_args(task[:200]),
                success=success,
                prompt_tokens=tokens.get("input_tokens", 0) if tokens else 0,
                completion_tokens=tokens.get("output_tokens", 0) if tokens else 0,
            )
        except Exception as e:
            logger.debug(f"emit tool.call 失败: {e}")

    def _sanitize_name(self, name: str) -> str:
        """清理名称，移除特殊字符"""
        import re
        # 保留字母、数字、中文、下划线和连字符
        safe = re.sub(r'[^\w一-鿿-]', '_', name)
        # 移除连续的下划线
        safe = re.sub(r'_+', '_', safe)
        # 移除首尾的下划线
        safe = safe.strip('_')
        return safe or "unnamed_client"

    async def check_available(self) -> bool:
        """检查容器中的 opencode CLI，不执行宿主机 opencode。"""
        return await self.sandbox.check_available()
