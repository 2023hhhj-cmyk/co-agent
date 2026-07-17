"""
Claude Code CLI 工具封装

通过子进程调用 Claude Code CLI 执行编码任务。
模型选择由 CC Switch 管理，本工具只负责调用命令行。
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from analytics import emit_event, hash_args

logger = logging.getLogger(__name__)


@dataclass
class ClaudeCodeResult:
    """Claude Code 执行结果"""
    success: bool
    output: str
    error: Optional[str] = None
    working_dir: Optional[str] = None
    tokens: dict = None  # ponytail: minimaxi token 用量（input/output/cache/cost）


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
        sandbox: Any = None,
    ):
        """
        初始化 ClaudeCodeTool

        Args:
            base_dir: 基础目录，客户项目将创建在此目录下
            timeout: 命令超时时间（秒），默认 600 秒（10 分钟）
            allowed_tools: 允许使用的工具列表，None 表示使用默认工具
            sandbox: SandboxedExecutor 实例，提供则使用沙箱执行
        """
        self.base_dir = base_dir
        self.timeout = timeout
        self.sandbox = sandbox
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

        # 沙箱模式（优先）
        if self.sandbox:
            logger.info(f"使用沙箱执行: mode={self.sandbox.mode}")
            sandbox_result = await self.sandbox.execute(task, context, cwd)
            # ponytail: 沙箱模式下尝试从 stdout 抓 token（如果沙箱内也用 --output-format json）
            sandbox_tokens = {}
            if sandbox_result.output:
                sandbox_tokens = self._parse_token_usage(sandbox_result.output)
            # ponytail: 工具调用埋点（含 minimaxi token）
            self._emit_tool_call_event(task, sandbox_result.success, sandbox_tokens)
            return ClaudeCodeResult(
                success=sandbox_result.success,
                output=sandbox_result.output,
                error=sandbox_result.error,
                working_dir=cwd,
                tokens=sandbox_tokens,
            )

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
                tokens = self._parse_token_usage(stdout_text)
                if tokens and tokens.get("input_tokens", 0) > 0:
                    logger.info(
                        f"minimaxi token: input={tokens['input_tokens']} "
                        f"output={tokens['output_tokens']} "
                        f"cache_read={tokens['cache_read_input_tokens']} "
                        f"cost_usd={tokens['total_cost_usd']:.4f}"
                    )
                # ponytail: 工具调用埋点（含 minimaxi token）
                self._emit_tool_call_event(full_task, True, tokens)
                return ClaudeCodeResult(
                    success=True,
                    output=stdout_text,
                    working_dir=cwd,
                    tokens=tokens,
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
        cmd = ["claude", "-p", task, "--output-format", "json"]

        # 添加允许的工具
        if self.allowed_tools:
            for tool in self.allowed_tools:
                cmd.extend(["--allowed-tools", tool])

        return cmd

    def _parse_token_usage(self, stdout_text: str) -> dict:
        """ponytail: 解析 Claude Code --output-format json 输出，提取 minimaxi token 数据。"""
        try:
            data = json.loads(stdout_text)
            usage = data.get("usage", {})
            model_usage = data.get("modelUsage", {})
            tokens = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_cost_usd": data.get("total_cost_usd", 0.0),
            }
            if model_usage:
                first_model = next(iter(model_usage.values()), {})
                tokens["input_tokens"] = first_model.get("inputTokens", 0)
                tokens["output_tokens"] = first_model.get("outputTokens", 0)
                tokens["cache_read_input_tokens"] = first_model.get("cacheReadInputTokens", 0)
                tokens["cache_creation_input_tokens"] = first_model.get("cacheCreationInputTokens", 0)
            else:
                tokens["input_tokens"] = usage.get("input_tokens", 0)
                tokens["output_tokens"] = usage.get("output_tokens", 0)
                tokens["cache_read_input_tokens"] = usage.get("cache_read_input_tokens", 0)
                tokens["cache_creation_input_tokens"] = usage.get("cache_creation_input_tokens", 0)
            return tokens
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"解析 token JSON 失败: {e}")
            return {}

    def _emit_tool_call_event(self, task: str, success: bool, tokens: dict) -> None:
        """ponytail: 统一工具调用埋点（含 minimaxi token）。所有 claude_code.execute() 调用都自动埋点。"""
        try:
            emit_event(
                "tool.call",
                tool_name="claude_code",
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
