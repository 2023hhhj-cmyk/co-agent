"""
Claude Code CLI 工具封装

通过子进程调用 Claude Code CLI 执行编码任务。
模型选择由 CC Switch 管理，本工具只负责调用命令行。
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeResult:
    """Claude Code 执行结果"""
    success: bool
    output: str
    error: Optional[str] = None
    working_dir: Optional[str] = None


class ClaudeCodeTool:
    """
    Claude Code CLI 工具封装

    通过子进程调用 claude 命令行执行编码任务。
    使用前请确保：
    1. 已安装 Claude Code CLI
    2. 已通过配置文件设置好模型提供商
    """

    def __init__(
        self,
        base_dir: str = "/root",
        timeout: int = 600,
        allowed_tools: Optional[list[str]] = None,
    ):
        """
        初始化 ClaudeCodeTool

        Args:
            base_dir: 基础目录，客户项目将创建在此目录下
            timeout: 命令超时时间（秒），默认 600 秒（10 分钟）
            allowed_tools: 允许使用的工具列表，None 表示使用默认工具
        """
        self.base_dir = base_dir
        self.timeout = timeout
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
    ) -> ClaudeCodeResult:
        """
        执行编码任务

        Args:
            task: 任务描述
            working_dir: 工作目录，None 则使用 base_dir
            context: 额外上下文信息

        Returns:
            ClaudeCodeResult: 执行结果
        """
        cwd = working_dir or self.base_dir

        # 确保工作目录存在
        os.makedirs(cwd, exist_ok=True)

        # 构建完整的任务描述
        full_task = task
        if context:
            full_task = f"{context}\n\n---\n\n{task}"

        # 构建命令参数
        cmd = self._build_command(full_task)

        logger.info(f"执行 Claude Code 任务，工作目录: {cwd}")
        logger.debug(f"任务描述: {full_task[:200]}...")

        try:
            # 执行命令
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ClaudeCodeResult(
                    success=False,
                    output="",
                    error=f"命令执行超时（{self.timeout}秒）",
                    working_dir=cwd,
                )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if process.returncode == 0:
                logger.info("Claude Code 任务执行成功")
                return ClaudeCodeResult(
                    success=True,
                    output=stdout_text,
                    working_dir=cwd,
                )
            else:
                logger.error(f"Claude Code 执行失败: {stderr_text}")
                return ClaudeCodeResult(
                    success=False,
                    output=stdout_text,
                    error=stderr_text or "命令执行失败",
                    working_dir=cwd,
                )

        except FileNotFoundError:
            error_msg = "未找到 claude 命令，请确保已安装 Claude Code CLI"
            logger.error(error_msg)
            return ClaudeCodeResult(
                success=False,
                output="",
                error=error_msg,
                working_dir=cwd,
            )
        except Exception as e:
            error_msg = f"执行 Claude Code 时发生错误: {str(e)}"
            logger.error(error_msg)
            return ClaudeCodeResult(
                success=False,
                output="",
                error=error_msg,
                working_dir=cwd,
            )

    async def execute_multiple(
        self,
        tasks: list[dict],
        max_concurrent: int = 8,
    ) -> list[ClaudeCodeResult]:
        """
        并行执行多个任务

        Args:
            tasks: 任务列表，每个任务是字典，包含 task、working_dir、context
            max_concurrent: 最大并发数

        Returns:
            list[ClaudeCodeResult]: 执行结果列表
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def run_with_semaphore(task_info: dict) -> ClaudeCodeResult:
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
                    ClaudeCodeResult(
                        success=False,
                        output="",
                        error=f"任务执行异常: {str(result)}",
                        working_dir=tasks[i].get("working_dir"),
                    )
                )
            else:
                final_results.append(result)

        return final_results

    def _build_command(self, task: str) -> list[str]:
        """构建命令参数列表"""
        cmd = ["claude", "-p", task]

        # 添加允许的工具
        if self.allowed_tools:
            for tool in self.allowed_tools:
                cmd.extend(["--allowed-tools", tool])

        return cmd

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
        """检查 Claude Code CLI 是否可用"""
        try:
            process = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0:
                version = stdout.decode("utf-8").strip()
                logger.info(f"Claude Code CLI 可用，版本: {version}")
                return True
            return False
        except FileNotFoundError:
            logger.warning("Claude Code CLI 不可用")
            return False
