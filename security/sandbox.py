"""opencode Docker executor: project-only mount, no host CLI fallback."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from config.settings import settings
from observability.metrics import sandbox_mode as sandbox_metric

logger = logging.getLogger(__name__)
DOCKER_IMAGE = "muzhu-sandbox:latest"
SANDBOX_TIMEOUT = 300
# Historical readonly tool set, kept for callers; host fallback is disabled.
READONLY_TOOLS = ["Read", "Glob", "Grep"]
FULL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
# 容器只接收这些模型配置，不转发平台其他凭证。
MODEL_ENV_KEYS = ("OPENCODE_API_KEY",)


@dataclass
class SandboxResult:
    success: bool
    output: str = ""
    error: str = ""
    sandbox_mode: str = "docker"
    raw: str = ""  # opencode --format json 原始 NDJSON，供 token 统计解析
    tokens: dict = field(default_factory=dict)


def _parse_opencode_events(raw: str) -> tuple[str, dict, str]:
    """把 opencode --format json 的 NDJSON 事件流解析成（文本, token 用量, 错误）。"""
    text_parts: list[str] = []
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_cost_usd": 0.0,
    }
    error = ""
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if etype == "text":
            value = part.get("text")
            if value:
                text_parts.append(str(value))
        elif etype == "step_finish":
            usage = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            tokens["input_tokens"] += int(usage.get("input") or 0)
            tokens["output_tokens"] += int(usage.get("output") or 0)
            cache = usage.get("cache") if isinstance(usage.get("cache"), dict) else {}
            tokens["cache_read_input_tokens"] += int(cache.get("read") or 0)
            tokens["cache_creation_input_tokens"] += int(cache.get("write") or 0)
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                tokens["total_cost_usd"] += float(cost)
        elif etype == "error":
            info = event.get("error") if isinstance(event.get("error"), dict) else {}
            data = info.get("data") if isinstance(info.get("data"), dict) else {}
            message = data.get("message") or info.get("name")
            error = str(message or "opencode 返回错误")
    return "\n".join(text_parts).strip(), tokens, error


class SandboxedExecutor:
    """All CLI execution happens inside Docker; unavailable Docker fails closed."""

    def __init__(self, base_dir="/root", docker_image=None,
                 timeout=SANDBOX_TIMEOUT, allowed_tools=None):
        self.base_dir = os.path.realpath(base_dir)
        self.docker_image = docker_image or settings.OC_SANDBOX_IMAGE
        self.timeout = timeout
        self.allowed_tools = list(FULL_TOOLS if allowed_tools is None else allowed_tools)

    def _check_docker(self) -> bool:
        available = shutil.which("docker") is not None
        sandbox_metric.set(int(available))
        return available

    @property
    def mode(self) -> str:
        # CLI presence only; daemon/image health checked when launching.
        return "docker" if self._check_docker() else "unavailable"

    def _validate_working_dir(self, working_dir=None) -> str:
        cwd = Path(working_dir or self.base_dir).resolve()
        roots = [Path(self.base_dir)]
        if not cwd.is_dir():
            raise ValueError("工作目录不存在")
        # Do not mount the whole /root, /tmp, or configured project parent.
        if cwd in roots or not any(root in cwd.parents for root in roots):
            raise ValueError("工作目录不在允许的项目子目录范围内")
        relative = cwd.relative_to(roots[0])
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("不能将宿主机隐藏配置目录作为项目挂载")
        if any(c in str(cwd) for c in (",", "\n", "\r")):
            raise ValueError("工作目录包含 Docker mount 不支持的字符")
        return str(cwd)

    def _local_opencode_key(self) -> str:
        """读取宿主机 opencode 已登录的 opencode-go 凭证（仅在环境变量缺省时兜底）。"""
        try:
            auth = json.loads((Path.home() / ".local/share/opencode/auth.json").read_text())
        except (OSError, ValueError):
            return ""
        entry = auth.get("opencode-go") if isinstance(auth, dict) else None
        if isinstance(entry, dict):
            key = entry.get("key")
            if isinstance(key, str):
                return key
        return ""

    def _model_env(self) -> dict:
        # 只转发模型凭证，绝不转发数据库、飞书、SSH 等平台其他凭证。
        result = {}
        for key in MODEL_ENV_KEYS:
            value = os.environ.get(key) or getattr(settings, key, "")
            if not value and key == "OPENCODE_API_KEY":
                value = self._local_opencode_key()
            if isinstance(value, str) and value:
                if any(c in value for c in ("\n", "\r", "\x00")):
                    raise ValueError("模型环境配置包含非法换行或空字符")
                result[key] = value
        if not result.get("OPENCODE_API_KEY"):
            raise ValueError("Docker CLI 缺少模型认证：请配置 OPENCODE_API_KEY")
        return result

    async def execute(self, task, context=None, working_dir=None, volume=None) -> SandboxResult:
        if not self._check_docker():
            return SandboxResult(False, error="Docker 不可用，拒绝在宿主机执行 opencode")
        return await self._execute_docker(task, context, working_dir, volume)

    async def _remove_container(self, name):
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "--force", name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("Docker 容器清理超时: %s", name)
        except OSError:
            logger.warning("无法清理 Docker 容器: %s", name)

    async def _execute_docker(self, task, context=None, working_dir=None, volume=None) -> SandboxResult:
        process = None
        name = "muzhu-oc-" + uuid.uuid4().hex
        try:
            if volume:
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", volume):
                    raise ValueError("容器名称不可用")
                cwd = "/workspace"
                mount = f"type=volume,source={volume},target=/workspace"
            else:
                cwd = self._validate_working_dir(working_dir)
                mount = f"type=bind,source={cwd},target=/workspace"
            model_env = self._model_env()
            prompt = f"{context}\n\n---\n{task}" if context else task
            # Prompts can contain host project paths; CLI only sees /workspace.
            prompt = prompt.replace(cwd, "/workspace")
            prompt = "项目根目录为 /workspace；所有项目文件修改应在此目录中完成。\n" + prompt
            # Private temporary env file avoids secrets in docker command arguments.
            with tempfile.TemporaryDirectory(prefix="muzhu-oc-env-") as env_dir:
                env_file = Path(env_dir) / "model.env"
                with open(env_file, "w", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
                    f.write("".join(f"{key}={value}\n" for key, value in model_env.items()))
                cmd = [
                    "docker", "run", "--rm", "--init", "--pull=never", "--name", name,
                    "--memory", settings.OC_SANDBOX_MEMORY,
                    "--cpus", str(settings.OC_SANDBOX_CPUS), "--pids-limit", "256",
                    # bridge permits API calls and dependency downloads; no host network.
                    "--network", "bridge", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m,mode=1777",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--env-file", str(env_file),
                    "--env", "HOME=/tmp/opencode-home",
                    "--env", "OPENCODE_DISABLE_AUTOUPDATE=1",
                    "--mount", mount,
                    "--workdir", "/workspace", self.docker_image,
                    "opencode", "run", prompt,
                    "--model", settings.OPENCODE_MODEL,
                    "--agent", settings.OPENCODE_AGENT,
                    "--format", "json",
                    "--auto",
                    "--dir", "/workspace",
                ]
                process = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    # Killing docker client alone does not stop the container.
                    await asyncio.shield(self._remove_container(name))
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return SandboxResult(False, error=f"Docker CLI 超时（{self.timeout}秒），已尝试清理容器")
            raw = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            text, tokens, event_error = _parse_opencode_events(raw)
            success = process.returncode == 0 and not event_error
            error = event_error or ("" if success else (stderr_text or "Docker CLI 执行失败"))
            return SandboxResult(
                success, output=text, error=error, raw=raw, tokens=tokens,
            )
        except (OSError, ValueError) as exc:
            logger.warning("Docker CLI 启动失败: %s", type(exc).__name__)
            return SandboxResult(False, error=str(exc))

    async def check_available(self) -> bool:
        """Probe installed image/daemon/CLI without invoking the model."""
        if not self._check_docker():
            return False
        name = "muzhu-oc-check-" + uuid.uuid4().hex
        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm", "--pull=never", "--name", name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                self.docker_image, "opencode", "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(process.communicate(), 15)
                return process.returncode == 0
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                await asyncio.shield(self._remove_container(name))
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return False
        except OSError:
            return False
