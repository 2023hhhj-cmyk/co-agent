"""
项目分析器 - 使用 opencode 智能分析项目目录

核心功能：
- 识别项目类型和技术栈
- 生成最优的 Dockerfile
- 生成 docker-compose.yml
- 确定健康检查路径
- 缓存分析结果
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from tools.code.opencode import OpenCodeTool, OpenCodeResult
from tools.deploy.cache import CacheManager

try:
    from analytics import emit_event, hash_args  # 埋点（运行数据采集）
except Exception:  # noqa: BLE001
    def emit_event(*_a, **_kw):  # type: ignore[no-redef]
        return None
    def hash_args(_a):  # type: ignore[no-redef]
        return ""

logger = logging.getLogger(__name__)


@dataclass
class ProjectInfo:
    """项目分析结果"""
    project_type: str = "unknown"        # node/python/static/mixed
    tech_stack: list[str] = field(default_factory=list)  # 技术栈列表
    dockerfile_content: str = ""         # Dockerfile 内容
    compose_content: str = ""            # docker-compose.yml 内容
    health_check_path: str = "/"         # 健康检查路径
    notes: str = ""                      # 注意事项
    entry_point: str = ""                # 入口文件
    build_command: str = ""              # 构建命令
    start_command: str = ""              # 启动命令


class ProjectAnalyzer:
    """
    项目分析器

    使用 opencode 智能分析项目目录，生成容器化配置。
    """

    def __init__(self, opencode: Optional[OpenCodeTool] = None, use_cache: bool = True):
        """
        初始化分析器

        Args:
            opencode: opencode 工具实例，如果为 None 则创建新实例
            use_cache: 是否使用缓存
        """
        self.opencode = opencode or OpenCodeTool(timeout=600)
        self.use_cache = use_cache
        self.cache = CacheManager() if use_cache else None

    async def analyze(self, project_dir: str) -> ProjectInfo:
        """
        分析项目目录，生成容器化配置

        Args:
            project_dir: 项目目录路径

        Returns:
            ProjectInfo: 项目分析结果
        """
        _args_hash = hash_args({"dir": project_dir})
        emit_event("tool.call", tool_name="analyzer.analyze", phase="start", tool_args_hash=_args_hash)
        _start = time.time()
        _ok = False
        try:
            if not os.path.isdir(project_dir):
                logger.error(f"项目目录不存在: {project_dir}")
                return ProjectInfo(
                    project_type="unknown",
                    notes=f"项目目录不存在: {project_dir}",
                )

            logger.info(f"开始分析项目目录: {project_dir}")

            # 检查缓存
            if self.use_cache and self.cache:
                cached = self.cache.get(project_dir)
                if cached:
                    logger.info(f"使用缓存结果: {project_dir}")
                    _ok = True
                    return ProjectInfo(**cached)

            # 构建任务描述
            task = self._build_task()

            # 调用 opencode 分析
            result = await self.opencode.execute(
                task=task,
                working_dir=project_dir,
            )

            if not result.success:
                logger.error(f"项目分析失败: {result.error}")
                return ProjectInfo(
                    project_type="unknown",
                    notes=f"分析失败: {result.error}",
                )

            # 解析结果
            project_info = self._parse_result(result)
            logger.info(f"项目分析完成: type={project_info.project_type}, tech={project_info.tech_stack}")
            _ok = True

            # 保存到缓存
            if self.use_cache and self.cache and project_info.project_type != "unknown":
                self.cache.set(project_dir, {
                    "project_type": project_info.project_type,
                    "tech_stack": project_info.tech_stack,
                    "dockerfile_content": project_info.dockerfile_content,
                    "compose_content": project_info.compose_content,
                    "health_check_path": project_info.health_check_path,
                    "notes": project_info.notes,
                    "entry_point": project_info.entry_point,
                    "build_command": project_info.build_command,
                    "start_command": project_info.start_command,
                })

            return project_info
        except Exception:
            emit_event("tool.call", tool_name="analyzer.analyze", phase="end",
                       duration_ms=int((time.time() - _start) * 1000), success=False)
            raise
        finally:
            if _ok:
                emit_event("tool.call", tool_name="analyzer.analyze", phase="end",
                           duration_ms=int((time.time() - _start) * 1000), success=True)

    def _build_task(self) -> str:
        """构建分析任务描述（优化版：更简洁，减少输出）"""
        return """分析项目目录，生成容器化配置。

输出 JSON（不要其他文本）：
{
    "project_type": "node|python|static|mixed",
    "tech_stack": ["框架名"],
    "entry_point": "入口文件",
    "build_command": "构建命令",
    "start_command": "启动命令",
    "health_check_path": "/health",
    "dockerfile": "Dockerfile内容",
    "compose": "docker-compose.yml内容",
    "notes": "注意事项"
}

要求：
1. project_type: node/python/static/mixed
2. dockerfile: 使用最优基础镜像，EXPOSE 声明端口
3. compose: 端口映射用 {{PORT}}，重启策略 unless-stopped
4. 如果已有 Dockerfile 可直接使用"""

    def _parse_result(self, result: OpenCodeResult) -> ProjectInfo:
        """解析 opencode 的输出"""
        output = result.output or ""

        # 尝试从输出中提取 JSON
        try:
            # 查找 JSON 块
            json_str = self._extract_json(output)
            if json_str:
                data = json.loads(json_str)
                return ProjectInfo(
                    project_type=data.get("project_type", "unknown"),
                    tech_stack=data.get("tech_stack", []),
                    dockerfile_content=data.get("dockerfile", ""),
                    compose_content=data.get("compose", ""),
                    health_check_path=data.get("health_check_path", "/"),
                    notes=data.get("notes", ""),
                    entry_point=data.get("entry_point", ""),
                    build_command=data.get("build_command", ""),
                    start_command=data.get("start_command", ""),
                )
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}")

        # 解析失败，返回默认值
        return ProjectInfo(
            project_type="unknown",
            notes=f"无法解析分析结果，原始输出:\n{output[:500]}",
        )

    def _extract_json(self, text: str) -> Optional[str]:
        """从文本中提取 JSON 块"""
        # 尝试查找 ```json ... ``` 代码块
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return text[start:end].strip()

        # 尝试查找 { ... } 块
        start = text.find("{")
        if start != -1:
            # 找到对应的 }
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]

        return None

    def generate_default_dockerfile(self, project_type: str, port: int = 8000) -> str:
        """生成默认的 Dockerfile（当 opencode 分析失败时使用）"""
        if project_type == "node":
            return f"""FROM node:18-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci --only=production

COPY . .

EXPOSE {port}

CMD ["npm", "start"]
"""
        elif project_type == "python":
            return f"""FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE {port}

CMD ["python", "app.py"]
"""
        elif project_type == "static":
            return f"""FROM nginx:alpine

COPY . /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
"""
        else:
            return f"""FROM ubuntu:22.04

WORKDIR /app

COPY . .

EXPOSE {port}

CMD ["bash"]
"""

    def generate_default_compose(self, service_name: str, port: int) -> str:
        """生成默认的 docker-compose.yml"""
        return f"""version: '3.8'

services:
  {service_name}:
    build: .
    container_name: {service_name}
    ports:
      - "{port}:{{{{PORT}}}}"
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
"""
