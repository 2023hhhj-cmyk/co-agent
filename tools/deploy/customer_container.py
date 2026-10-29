"""每个客户一个长期容器，项目文件放在它的 /workspace 卷里。

容器只提供工作区。PostgreSQL、Redis 和 Agent 平台仍是共享服务，不在这里安装。
"""

import asyncio
import hashlib
import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = "/workspace"
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


@dataclass
class CustomerWorkspace:
    container_name: str
    volume_name: str
    workspace: str = WORKSPACE_ROOT


def container_name_for(label: str) -> str:
    """Docker 名称只保留安全字符。中文等标识用稳定哈希区分。"""
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "", label or "").strip(".-")
    if not slug or not slug[0].isalnum():
        slug = hashlib.sha256((label or "client").encode()).hexdigest()[:16]
    return f"muzhu-customer-{slug[:40]}"


def validate_docker_name(name: str) -> str:
    if not name or not _NAME_RE.fullmatch(name):
        raise ValueError("容器名称不可用")
    return name


def workspace_path(path: str) -> str:
    """只允许 /workspace 内部的路径。"""
    raw = (path or "").strip().replace("\\", "/")
    if not raw.startswith("/"):
        raw = f"{WORKSPACE_ROOT}/{raw.lstrip('/')}"
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or parts[0] != "workspace" or ".." in parts:
        raise ValueError("路径超出客户工作区")
    return "/" + "/".join(parts)


class CustomerContainers:
    """创建并访问客户容器。不在容器内安装数据库。"""

    def __init__(self, runner=None, image: Optional[str] = None):
        self.runner = runner
        self.image = image or settings.OC_SANDBOX_IMAGE
        # 同一容器的创建必须串行：inspect 与 run 之间有 await 窗口
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    async def ensure(self, label: str) -> CustomerWorkspace:
        name = validate_docker_name(container_name_for(label))
        async with self._lock_for(name):
            return await self._ensure_locked(name)

    async def _ensure_locked(self, name: str) -> CustomerWorkspace:
        await self._docker(["volume", "create", name])
        code, out, _err = await self._docker(
            ["inspect", "-f", "{{.State.Running}}", name], check=False,
        )
        if code != 0:
            try:
                await self._docker([
                    "run", "-d", "--name", name, "--restart", "unless-stopped",
                    "--network", "bridge", "--memory", "512m", "--pids-limit", "256",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--mount", f"type=volume,source={name},target={WORKSPACE_ROOT}",
                    "--entrypoint", "sleep", self.image, "infinity",
                ])
            except RuntimeError as exc:
                # 并发下另一个调用可能刚好建好容器，视为成功
                if "already in use" not in str(exc) and "Conflict" not in str(exc):
                    raise
                await self._docker(["start", name])
        elif out.strip() != "true":
            await self._docker(["start", name])
        return CustomerWorkspace(container_name=name, volume_name=name)

    async def write_file(self, container_name: str, path: str, text: str) -> None:
        name = validate_docker_name(container_name)
        target = workspace_path(path)
        parent = target.rsplit("/", 1)[0] or WORKSPACE_ROOT
        script = f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(target)}"
        await self._docker(["exec", "-i", name, "sh", "-c", script], stdin=text.encode("utf-8"))

    async def read_file(self, container_name: str, path: str, limit: int = 2000) -> str:
        name = validate_docker_name(container_name)
        target = workspace_path(path)
        _code, out, err = await self._docker(["exec", name, "cat", target], check=False)
        if err and not out:
            return ""
        return out[:limit]

    async def exists(self, container_name: str, path: str) -> bool:
        name = validate_docker_name(container_name)
        try:
            target = workspace_path(path)
        except ValueError:
            return False
        code, _out, _err = await self._docker(["exec", name, "test", "-f", target], check=False)
        return code == 0

    async def makedirs(self, container_name: str, path: str) -> None:
        name = validate_docker_name(container_name)
        target = workspace_path(path)
        await self._docker(["exec", name, "mkdir", "-p", target])

    async def _docker(self, args: list[str], stdin: Optional[bytes] = None, check: bool = True):
        if self.runner is not None:
            code, out, err = await self.runner(args, stdin)
        else:
            code, out, err = await _run_docker(args, stdin)
        if check and code != 0:
            detail = (err or out or "docker 命令失败").strip()
            raise RuntimeError(detail[:300])
        return code, out, err


async def _run_docker(args: list[str], stdin: Optional[bytes]) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError("Docker 不可用，无法创建客户容器") from exc
    out, err = await process.communicate(stdin)
    return (
        process.returncode or 0,
        (out or b"").decode("utf-8", errors="replace"),
        (err or b"").decode("utf-8", errors="replace"),
    )


class HostFiles:
    """测试和尚未分配容器时，在已有宿主机目录里读写。"""

    def __init__(self, root: str):
        self.root = os.path.realpath(root)
        self.volume = ""

    def _under(self, path: str) -> str:
        target = os.path.realpath(path)
        try:
            inside = os.path.commonpath([self.root, target]) == self.root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("路径超出客户工作区")
        return target

    async def makedirs(self, path: str) -> None:
        os.makedirs(self._under(path), exist_ok=True)

    async def write(self, path: str, text: str) -> None:
        target = path if os.path.isabs(path) else os.path.join(self.root, path)
        parent = os.path.dirname(target)
        os.makedirs(parent, exist_ok=True)
        self._under(parent)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    async def read(self, path: str, limit: int = 2000) -> str:
        if not path or not os.path.isfile(path):
            return ""
        self._under(path)
        with open(path, encoding="utf-8") as handle:
            return handle.read(limit)

    async def exists(self, path: str) -> bool:
        try:
            self._under(path if os.path.isabs(path) else os.path.join(self.root, path))
        except ValueError:
            return False
        return os.path.isfile(path if os.path.isabs(path) else os.path.join(self.root, path))


class ContainerFiles:
    """在客户容器的 /workspace 里读写。"""

    def __init__(self, containers: CustomerContainers, container_name: str, volume_name: str):
        self.containers = containers
        self.container_name = validate_docker_name(container_name)
        self.volume = validate_docker_name(volume_name)
        self.root = WORKSPACE_ROOT

    async def makedirs(self, path: str) -> None:
        await self.containers.makedirs(self.container_name, path)

    async def write(self, path: str, text: str) -> None:
        await self.containers.write_file(self.container_name, path, text)

    async def read(self, path: str, limit: int = 2000) -> str:
        return await self.containers.read_file(self.container_name, path, limit)

    async def exists(self, path: str) -> bool:
        return await self.containers.exists(self.container_name, path)
