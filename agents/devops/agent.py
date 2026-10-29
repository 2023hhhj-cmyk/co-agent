"""
运维工程师 Agent - 重写版本

职责：
1. 接收 Developer 的产出，自动部署应用
2. 使用 opencode 分析项目，生成 Dockerfile
3. Docker 容器化部署
4. Nginx 反向代理配置
5. 持续监控和异常自愈
6. Web 管理接口预留

工作流程：
- 收到部署请求 → 分析项目 → 构建镜像 → 启动容器 → 配置 Nginx → 健康检查 → 监控
"""

import asyncio
import glob
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Optional

from urllib.parse import urlparse

from agents.base import BaseAgent
from config.settings import settings
from models.agent import ActionDef, AgentRole, ReactMode
from models.deploy import DeployRecord, ServiceStatus
from models.message import Message
from tools.code.opencode import OpenCodeTool
from tools.deploy.analyzer import ProjectAnalyzer
from tools.deploy.docker import DockerManager, PortManager
from tools.deploy.monitor import ServiceMonitor
from tools.deploy.nginx import NginxManager
from tools.llm.client import LLMClient

logger = logging.getLogger(__name__)

# 运维工程师角色定义
DEVOPS_ROLE = AgentRole(
    name="devops",
    profile="运维工程师",
    goal="部署、监控和维护应用，确保系统稳定运行",
    constraints="确保部署安全，监控全面，故障快速恢复",
    tools=["docker", "nginx", "monitoring", "opencode"],
    actions=[
        ActionDef(
            name="deploy",
            description="部署应用",
            prompt_template="请部署以下应用：\n{application}\n\n项目目录：{project_dir}",
        ),
        ActionDef(
            name="monitor",
            description="监控服务状态",
            prompt_template="请检查以下服务的运行状态：\n{service_name}",
        ),
        ActionDef(
            name="fix",
            description="修复异常服务",
            prompt_template="请修复以下异常服务：\n{service_name}\n\n错误信息：{error}",
        ),
        ActionDef(
            name="report",
            description="生成运维报告",
            prompt_template="请生成运维报告",
        ),
    ],
    watch={"review_code", "deploy_request", "deploy_fix", "ceo_task_assignment"},
    react_mode=ReactMode.REACT,
    max_react_loop=3,
)


class DevOpsAgent(BaseAgent):
    """运维工程师 Agent"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        base_dir: str = "/root",
        **kwargs,
    ):
        logger.info("DEBUG DevOpsAgent __init__ CALLED")
        super().__init__(
            role=DEVOPS_ROLE,
            llm_client=llm_client,
            **kwargs,
        )

        self.base_dir = base_dir

        # 初始化工具
        self.opencode = OpenCodeTool(
            base_dir=base_dir,
            timeout=300,
        )
        self.analyzer = ProjectAnalyzer(self.opencode)
        self.database = None
        self.port_manager = PortManager(start=8001, end=9000)
        self.docker = DockerManager(self.port_manager)
        self.public_base_url = settings.PUBLIC_BASE_URL.rstrip("/")
        # nginx 配置是读-改-写，必须串行化，否则并发部署会丢 location 块
        self._nginx_lock = threading.Lock()
        # 一个任务只执行一次部署；_react 循环会反复调用 _think
        self._task_done = False
        self.nginx = NginxManager(domain=urlparse(self.public_base_url).hostname or "localhost")
        self.monitor = ServiceMonitor(
            docker_manager=self.docker,
            check_interval=30,
            max_restart_attempts=3,
        )

        # 设置监控回调
        self.monitor.set_callbacks(
            on_failure=self._on_service_failure,
            on_restart=self._on_service_restart,
            on_recovered=self._on_service_recovered,
        )

        # 已部署的服务
        self.deployed_services: dict[str, DeployRecord] = {}  # service_name -> DeployRecord

        # 飞书客户端（用于通知 CEO）
        self._feishu_client = None
        self._ceo_chat_id = None

    def set_feishu_client(self, feishu_client, ceo_chat_id: str):
        """设置飞书客户端（用于通知）"""
        self._feishu_client = feishu_client
        self._ceo_chat_id = ceo_chat_id

    def bind_database(self, database) -> None:
        """启动后注入数据库，端口分配和部署记录都走它。"""
        self.database = database
        self.port_manager.database = database

    async def restore_deployments(self) -> None:
        """把未停止的部署记录灌回内存。容器不在了就标失败，不自动拉起。"""
        if not self.database:
            return
        rows = await self.database.fetch(
            "SELECT * FROM deployments WHERE status <> 'stopped'"
        )
        for row in rows:
            record = DeployRecord(
                id=row["deployment_id"] or row["service_name"],
                service_name=row["service_name"],
                client_name=row["client_name"] or row["client_id"],
                client_id=row["client_id"] or "",
                client_dir=row["project_dir"] or "",
                project_type=row["kind"] or "static",
                container_name=row["container_name"] or "",
                port=int(row["port"] or 0),
                url=row["url"] or "",
                status=row["status"] or "running",
                error_message=row["error_message"],
            )
            if record.container_name:
                running = await self.docker.is_running(record.container_name)
                if not running:
                    record.mark_failed("container missing after restart")
                    await self._save_deployment(record, row["client_id"] or "", row["site_root"] or "")
            self.deployed_services[record.id] = record
        await self.port_manager.load_allocated()
        logger.info("restored %s deployments", len(self.deployed_services))

    async def _save_deployment(self, record: DeployRecord, client_id: str = "", site_root: str = "") -> None:
        """把一条部署记录写入数据库。"""
        if client_id:
            record.client_id = client_id
        self.deployed_services[record.id] = record
        if not self.database:
            return
        deployment_id = getattr(record, "deployment_id", "") or record.id
        await self.database.execute(
            "INSERT INTO deployments ("
            "deployment_id, service_name, client_id, client_name, kind, port, container_name, "
            "site_root, project_dir, url, status, error_message, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW()) "
            "ON CONFLICT (deployment_id) DO UPDATE SET "
            "client_id = EXCLUDED.client_id, client_name = EXCLUDED.client_name, "
            "kind = EXCLUDED.kind, port = EXCLUDED.port, "
            "container_name = EXCLUDED.container_name, site_root = EXCLUDED.site_root, "
            "project_dir = EXCLUDED.project_dir, url = EXCLUDED.url, "
            "status = EXCLUDED.status, error_message = EXCLUDED.error_message, "
            "updated_at = NOW()",
            deployment_id,
            record.service_name,
            client_id or record.client_name,
            record.client_name,
            record.project_type or "static",
            int(record.port or 0),
            record.container_name or "",
            site_root,
            record.client_dir or "",
            record.url or "",
            record.status.value,
            record.error_message,
        )

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """思考阶段"""
        logger.info(f"DEBUG devops._think called with {len(messages)} messages")
        if not messages:
            return None

        if getattr(self, "_task_done", False):
            # 已经部署过（成功或失败），结束本任务的 Think-Act 循环
            return None

        last_message = messages[-1]
        logger.info(f"DEBUG devops._think last_message.cause_by={last_message.cause_by if last_message else None}")

        if last_message.cause_by == "deploy_fix":
            return self.role.get_action_by_name("fix")

        # ponytail: 任何收到消息都尝试部署（plan 触发 / review_code 触发 / deploy_request 触发都走 deploy）
        return self.role.get_action_by_name("deploy")

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """执行阶段"""
        messages = context.get("messages", []) if context else []
        last_message = messages[-1] if messages else None

        if action.name == "deploy":
            result = await self._deploy(last_message)
            self._task_done = True
            return result
        elif action.name == "monitor":
            return await self._monitor(last_message)
        elif action.name == "fix":
            return await self._fix(last_message)
        elif action.name == "report":
            return await self._report()
        else:
            return Message(
                content=f"未知动作：{action.name}",
                cause_by="error",
                sent_from=self.agent_id,
            )

    async def _deploy(self, last_message: Optional[Message]) -> Message:
        """
        部署应用

        完整流程：
        1. 解析项目目录
        2. 分析项目类型
        3. 生成 Dockerfile 和 docker-compose.yml
        4. 构建镜像
        5. 分配端口
        6. 启动容器
        7. 配置 Nginx
        8. 健康检查
        9. 启动监控
        """
        if not last_message:
            return Message(
                content="未收到部署请求",
                cause_by="error",
                sent_from=self.agent_id,
            )

        # 解析项目信息：优先用 CEO 给的卷宿主路径（DevOps 在宿主机上，读不到 /workspace）
        client_id = str((last_message.metadata or {}).get("client_id") or "").strip()
        host_dir = str((last_message.metadata or {}).get("host_dir") or "").strip()
        if not client_id or not host_dir:
            return Message(content="部署被拒绝：缺少 client_id 或 host_dir", cause_by="error", sent_from=self.agent_id)
        if not self._valid_host_dir(host_dir, client_id):
            return Message(content="部署被拒绝：源目录不在允许范围内", cause_by="error", sent_from=self.agent_id)
        project_dir = os.path.realpath(host_dir)
        client_name = (last_message.metadata or {}).get("client_name") or client_id
        self._deploy_client_id = client_id
        service_name = self._generate_service_name(client_name)

        # ponytail: 先看上游声明的产物，再决定用静态托管还是容器化。
        # 有声明就不扫目录；没有声明才回退到旧的 index.html 探测。
        declared = self._declared_upstream_files(last_message)
        verdict = self._looks_static(declared)

        if verdict is True:
            missing = [name for name in declared if not self._file_present(project_dir, name)]
            if missing:
                return Message(
                    content=f"静态部署被拒绝：上游声明了这些文件，但磁盘上不存在 → {missing[:10]}",
                    cause_by="error",
                    sent_from=self.agent_id,
                )
            logger.info(f"上游声明为纯静态项目（{len(declared)} 个文件），跳过容器化: {declared[:8]}")
            return await self._static_deploy(project_dir, client_name, service_name)

        if verdict is False:
            logger.info("上游声明包含后端/构建文件，走容器化部署，跳过静态判断")

        if verdict is None:
            index_html = os.path.join(project_dir, "index.html")
            if os.path.exists(index_html):
                return await self._static_deploy(project_dir, client_name, service_name)

        logger.info(f"开始部署: {service_name}，项目目录: {project_dir}")

        # 创建部署记录
        record = DeployRecord(
            service_name=service_name,
            client_name=client_name,
            client_dir=project_dir,
            status=ServiceStatus.DEPLOYING,
        )

        try:
            # 第 1 步：分析项目
            logger.info(f"[1/7] 分析项目: {project_dir}")
            project_info = await self.analyzer.analyze(project_dir)

            record.project_type = project_info.project_type
            record.tech_stack = project_info.tech_stack
            record.health_check_path = project_info.health_check_path

            # 第 2 步：生成配置文件（如果 opencode 没有生成）
            if not project_info.dockerfile_content:
                logger.info("opencode 未生成 Dockerfile，使用默认配置")
                project_info.dockerfile_content = self.analyzer.generate_default_dockerfile(
                    project_info.project_type
                )

            if not project_info.compose_content:
                logger.info("opencode 未生成 docker-compose.yml，使用默认配置")
                project_info.compose_content = self.analyzer.generate_default_compose(
                    service_name, 8000
                )

            # 第 3 步：写入配置文件
            logger.info("[2/7] 写入配置文件")
            await self._write_config_files(project_dir, project_info, service_name)

            # 第 4 步：构建镜像
            image_name = f"loveboos/{service_name}"
            logger.info(f"[3/7] 构建镜像: {image_name}")
            record.image_name = image_name

            build_ok = await self.docker.build(image_name, project_dir)
            if not build_ok:
                raise Exception("镜像构建失败")

            # 第 5 步：分配端口并启动容器
            port = await self.port_manager.allocate(record.id)
            container_name = f"loveboos-{service_name}"
            logger.info(f"[4/7] 启动容器: {container_name}，端口: {port}")

            record.port = port
            record.container_name = container_name

            # 确定容器内部端口
            internal_port = self._detect_internal_port(project_info)

            run_ok = await self.docker.run(
                container_name=container_name,
                image_name=image_name,
                port=port,
                internal_port=internal_port,
            )
            if not run_ok:
                raise Exception("容器启动失败")

            # 第 6 步：配置 Nginx
            logger.info(f"[5/7] 配置 Nginx: {service_name} -> port {port}")
            nginx_ok = await self.nginx.add_service(service_name, port)
            if nginx_ok:
                await self.nginx.reload()

            # 第 7 步：健康检查
            logger.info("[6/7] 执行健康检查")
            await asyncio.sleep(3)  # 等待服务启动

            health = await self.monitor.check_health(record)
            record.update_health(health)

            if health.status.value != "healthy":
                logger.warning(f"健康检查未通过，但服务已启动: {health.error}")

            # 第 8 步：启动监控
            logger.info("[7/7] 启动持续监控")
            record.mark_running()
            await self._save_deployment(record, self._deploy_client_id)
            await self.monitor.start_monitoring(record)

            # 生成访问 URL
            url = f"{self.public_base_url}/{service_name}"
            record.url = url

            logger.info(f"部署成功: {service_name} -> {url}")

            # 返回部署报告
            return Message(
                content=self._generate_deploy_report(record),
                cause_by="deploy_complete",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "deploy",
                    "service_name": service_name,
                    "url": url,
                    "port": port,
                    "project_type": project_info.project_type,
                    "status": "success",
                    "files": [],
                    "acceptance_text": "部署完成",
                    "passed": True,
                },
            )

        except Exception as e:
            logger.error(f"部署失败: {service_name} - {e}")
            record.mark_failed(str(e))
            await self._save_deployment(record, self._deploy_client_id)

            return Message(
                content=f"部署失败: {str(e)}",
                cause_by="deploy_failed",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "deploy",
                    "service_name": service_name,
                    "error": str(e),
                    "status": "failed",
                },
            )

    async def _static_deploy(
        self, project_dir: str, client_name: str, service_name: str
    ) -> Message:
        """ponytail: 智能部署——根据项目类型自动选 Node/Python/纯静态。"""
        from pathlib import Path
        project = Path(project_dir)
        if (project / "package.json").exists():
            return await self._deploy_node_project(project_dir, client_name, service_name)
        elif (project / "requirements.txt").exists() or (project / "pyproject.toml").exists():
            return await self._deploy_python_project(project_dir, client_name, service_name)
        else:
            return await self._deploy_static(project_dir, client_name, service_name)

    async def _deploy_node_project(
        self, project_dir: str, client_name: str, service_name: str
    ) -> Message:
        """ponytail: Node/Vue/React 项目：npm install + build + 部署 dist/。"""
        import subprocess
        client_id = getattr(self, "_deploy_client_id", "") or client_name
        deployment_id, safe_name, public_path, site_root = self._deployment_target(client_id, service_name)

        async def _run(cmd, timeout=300):
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=project_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return False, "", f"timeout after {timeout}s"
            return proc.returncode == 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

        ok, _, err = await _run(["npm", "install", "--prefer-offline", "--no-audit", "--no-fund"], timeout=300)
        if not ok:
            return Message(content=f"npm install 失败:\n{err[:500]}", cause_by="deploy_failed", sent_from=self.agent_id)

        ok, stdout, err = await _run(["npm", "run", "build"], timeout=300)
        if not ok:
            return Message(content=f"npm run build 失败:\n{stdout[-300:]}\n{err[-500:]}", cause_by="deploy_failed", sent_from=self.agent_id)

        # 找 build 输出目录
        dist_dir = None
        for candidate in ["dist", "build", "public", ".next", "out"]:
            p = os.path.join(project_dir, candidate)
            if os.path.isdir(p):
                dist_dir = p
                break
        if not dist_dir:
            return Message(content="build 完成但找不到 dist/ build/ public/ .next/ out/ 任何输出目录", cause_by="deploy_failed", sent_from=self.agent_id)

        # 复制 dist/ 到 nginx 目录
        import shutil
        if os.path.exists(site_root):
            shutil.rmtree(site_root)
        shutil.copytree(dist_dir, site_root)

        # 写 nginx + reload
        self._add_nginx_location(public_path, site_root)
        status = self._reload_nginx()
        record = DeployRecord(
            id=deployment_id, service_name=safe_name, client_name=client_name, client_dir=project_dir,
            project_type="node", url=f"{self.public_base_url}/{public_path}/",
        )
        record.mark_running()
        await self._save_deployment(record, self._deploy_client_id, site_root)
        return Message(
            content=f"[Node 项目部署完成] {status}\n源: {project_dir}\n产物: {dist_dir}\n目标: {site_root}\nURL: {self.public_base_url}/{public_path}/",
            cause_by="deploy_complete", sent_from=self.agent_id,
            metadata={"action": "deploy_node", "service_name": service_name, "site_url": f"{self.public_base_url}/{public_path}/", "site_root": site_root},
        )

    async def _deploy_python_project(
        self, project_dir: str, client_name: str, service_name: str
    ) -> Message:
        """ponytail: Python 项目：跳过（v3 暂不实现完整 Python 部署）。"""
        return Message(
            content="Python 项目部署暂未实现，请手动部署或先用 _deploy_static 路径。",
            cause_by="deploy_skipped", sent_from=self.agent_id,
        )

    def _add_nginx_location(self, safe_name: str, site_root: str) -> None:
        """ponytail: 写 location 块到现有 loveboos-ai.conf 的 marker 后。"""
        lock = getattr(self, "_nginx_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._nginx_lock = lock
        with lock:
            self._add_nginx_location_locked(safe_name, site_root)

    def _add_nginx_location_locked(self, safe_name: str, site_root: str) -> None:
        nginx_conf = getattr(self, "nginx_conf_path", "/etc/nginx/conf.d/loveboos-ai.conf")
        # 删旧的同 safe_name 块
        with open(nginx_conf) as f:
            content = f.read()
        content = re.sub(
            rf"    # auto-deploy: {re.escape(safe_name)}\n    location[^{{]*\{{[^}}]*}}\n",
            "", content,
        )
        location_block = (
            f"    # auto-deploy: {safe_name}\n"
            f"    location /{safe_name} {{\n"
            f"        alias {site_root};\n"
            f"        index index.html;\n"
            f"        try_files $uri $uri/ =404;\n"
            f"    }}\n"
        )
        m = "    # 服务配置将自动添加到此处\n"
        if m in content:
            content = content.replace(m, m + location_block)
        else:
            content += location_block
        with open(nginx_conf, "w") as f:
            f.write(content)

    def _reload_nginx(self) -> str:
        """ponytail: nginx -t + reload。"""
        import subprocess
        try:
            subprocess.run(["nginx", "-t"], check=True, capture_output=True, timeout=10)
            subprocess.run(["systemctl", "reload", "nginx"], check=True, capture_output=True, timeout=10)
            return "nginx reload 成功"
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode() if e.stderr else str(e)
            return f"nginx reload 失败: {err[:300]}"

    async def _deploy_static(
        self, project_dir: str, client_name: str, service_name: str
    ) -> Message:
        """
        ponytail: 快速静态部署——跳过 Docker，直接 nginx 静态托管。
        用于 client_dir 里已经有 index.html 的场景（fallback LLM 已生成）。
        """
        import shutil
        import subprocess

        # 目标路径：/var/www/muzhu-ai/{service_name}
        client_id = getattr(self, "_deploy_client_id", "") or client_name
        deployment_id, safe_name, public_path, site_root = self._deployment_target(client_id, service_name)
        os.makedirs(site_root, exist_ok=True)

        # 复制 index.html + 任何附属文件
        for fname in os.listdir(project_dir):
            src = os.path.join(project_dir, fname)
            dst = os.path.join(site_root, fname)
            if os.path.isfile(src):
                shutil.copy2(src, dst)

        # 写 nginx location 块——必须是 server 块内的合法指令
        # （直接 append 到 conf.d 文件末尾会落在 server 块之外，nginx -t 报错）
        self._add_nginx_location(public_path, site_root)

        # reload nginx
        try:
            subprocess.run(
                ["nginx", "-t"], check=True, capture_output=True, timeout=10
            )
            subprocess.run(
                ["systemctl", "reload", "nginx"], check=True, capture_output=True, timeout=10
            )
            status_msg = f"已部署到 {self.public_base_url}/{public_path}/"
        except subprocess.CalledProcessError as e:
            status_msg = f"nginx reload 失败: {e.stderr.decode() if e.stderr else e}"

        record = DeployRecord(
            id=deployment_id, service_name=safe_name, client_name=client_name, client_dir=project_dir,
            project_type="static", url=f"{self.public_base_url}/{public_path}/",
        )
        record.mark_running()
        await self._save_deployment(record, self._deploy_client_id, site_root)
        return Message(
            content=f"[静态部署完成] {status_msg}\n源: {project_dir}\n目标: {site_root}",
            cause_by="deploy_complete",
            sent_from=self.agent_id,
            metadata={
                "action": "static_deploy",
                "service_name": service_name,
                "site_url": f"{self.public_base_url}/{public_path}/",
                "site_root": site_root,
            },
        )


    def _deployment_target(self, client_id: str, service_name: str) -> tuple[str, str, str, str]:
        """每次部署一个新编号，目录和网址都带上客户编号。"""
        deployment_id = "d" + uuid.uuid4().hex[:12]
        safe_client = self._sanitize_name(client_id or "unknown")
        safe_name = self._sanitize_name(service_name)
        public_path = f"c/{safe_client}/{deployment_id}"
        site_root = f"/var/www/muzhu-ai/{public_path}"
        return deployment_id, safe_name, public_path, site_root

    def _sanitize_name(self, name: str) -> str:
        """清理名称做 URL 段。"""
        import re
        s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "site").lower()).strip("-")
        return s or "site"

    async def _monitor(self, last_message: Optional[Message]) -> Message:
        """监控服务状态"""
        services_status = await self.monitor.get_all_status()

        if not services_status:
            content = "当前没有正在监控的服务"
        else:
            lines = ["# 服务监控状态\n"]
            for svc in services_status:
                status_icon = "✅" if svc["health"] == "healthy" else "❌"
                lines.append(f"{status_icon} **{svc['service_name']}**")
                lines.append(f"   - URL: {svc['url']}")
                lines.append(f"   - 状态: {svc['status']}")
                lines.append(f"   - 健康: {svc['health']}")
                lines.append(f"   - 重启次数: {svc['restart_count']}")
                if svc["error"]:
                    lines.append(f"   - 错误: {svc['error']}")
                lines.append("")
            content = "\n".join(lines)

        return Message(
            content=content,
            cause_by="monitor_report",
            sent_from=self.agent_id,
            send_to={"*"},
            metadata={"action": "monitor", "services": services_status},
        )

    async def _fix(self, last_message: Optional[Message]) -> Message:
        """修复异常服务"""
        if not last_message:
            return Message(
                content="未收到修复请求",
                cause_by="error",
                sent_from=self.agent_id,
            )

        deployment_id = last_message.metadata.get("deployment_id")
        service = self.get_deployment(deployment_id)
        if not service:
            return Message(
                content=f"未找到部署: {deployment_id}",
                cause_by="error",
                sent_from=self.agent_id,
            )
        service_name = service.service_name
        logger.info(f"尝试修复服务: {service_name}")

        # 尝试重启
        success = await self.docker.restart(service.container_name)

        if success:
            # 等待服务启动
            await asyncio.sleep(5)

            # 检查健康状态
            health = await self.monitor.check_health(service)
            service.update_health(health)

            if health.status.value == "healthy":
                service.mark_running()
                return Message(
                    content=f"服务 {service_name} 修复成功，已恢复正常",
                    cause_by="fix_complete",
                    sent_from=self.agent_id,
                    send_to={"*"},
                    metadata={"action": "fix", "service_name": service_name, "status": "success"},
                )

        return Message(
            content=f"服务 {service_name} 修复失败，可能需要人工介入",
            cause_by="fix_failed",
            sent_from=self.agent_id,
            send_to={"*"},
            metadata={"action": "fix", "service_name": service_name, "status": "failed"},
        )

    async def _report(self) -> Message:
        """生成运维报告"""
        services = list(self.deployed_services.values())

        if not services:
            content = "当前没有已部署的服务"
        else:
            lines = ["# 运维报告\n"]
            lines.append(f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"**服务总数**: {len(services)}")
            lines.append("")

            running = sum(1 for s in services if s.status == ServiceStatus.RUNNING)
            failed = sum(1 for s in services if s.status == ServiceStatus.FAILED)
            lines.append(f"- ✅ 运行中: {running}")
            lines.append(f"- ❌ 失败: {failed}")
            lines.append("")

            lines.append("## 服务详情\n")
            for service in services:
                lines.append(service.to_summary())

            content = "\n".join(lines)

        return Message(
            content=content,
            cause_by="ops_report",
            sent_from=self.agent_id,
            send_to={"*"},
            metadata={"action": "report", "services_count": len(services)},
        )

    # ========== 辅助方法 ==========

    # P1 #11 修复：项目路径白名单
    # 攻击场景：LLM 被 prompt 注入后输出 {"client_dir": "/etc"} → 触发 docker build /etc
    # 修复：所有 client_dir 必须落在 /root/<safe_name>/ 下
    # 注：可通过环境变量 MUZHU_PROJECT_BASE 覆盖（用于测试环境）
    import os as _os
    _ALLOWED_PROJECT_DIR_PREFIX = _os.environ.get("MUZHU_PROJECT_BASE", "/root/")

    @classmethod
    def _validate_project_dir(cls, raw_dir: str, safe_name: str) -> str:
        """校验项目路径合法（白名单 + 符号链接解析）

        Args:
            raw_dir: 待校验的路径
            safe_name: 已清理的客户名（用于构造白名单路径）

        Returns:
            校验通过的真实路径

        Raises:
            ValueError: 路径不安全
        """
        import os

        if not raw_dir or not isinstance(raw_dir, str):
            raise ValueError(f"[Security] 项目目录为空")

        # 解析符号链接和相对路径
        try:
            real_path = os.path.realpath(raw_dir)
        except Exception as e:
            raise ValueError(f"[Security] 路径解析失败: {e}")

        # 白名单检查：必须落在 <BASE>/<safe_name>/ 下
        allowed_prefix = os.path.join(cls._ALLOWED_PROJECT_DIR_PREFIX, safe_name)
        allowed_prefix_real = os.path.realpath(allowed_prefix)
        if not real_path.startswith(allowed_prefix_real + os.sep) and real_path != allowed_prefix_real:
            raise ValueError(
                f"[Security] 项目目录不在白名单: {raw_dir} (解析为 {real_path}) "
                f"必须在 {allowed_prefix_real} 下"
            )

        return real_path

    def _valid_host_dir(self, path: str, client_id: str = "") -> bool:
        """只允许本客户的数据目录，拒绝链接跳到目录外。"""
        try:
            real = os.path.realpath(path)
        except Exception:
            return False
        if not os.path.isdir(real):
            return False
        volume_root = "/var/lib/docker/volumes/"
        source_root = "/var/www/muzhu-ai/sources/"
        if real.startswith(volume_root) and real.endswith("/_data"):
            return True
        expected = os.path.realpath(os.path.join(source_root, self._sanitize_name(client_id)))
        return bool(client_id) and (real == expected or real.startswith(expected + os.sep))

    def _declared_upstream_files(self, message: Message) -> list[str]:
        """读取上游声明的产物清单（兼容 dict 与纯字符串两种形式）。"""
        raw = (message.metadata or {}).get("upstream_files") or []
        names: list[str] = []
        for item in raw:
            path = item.get("path") if isinstance(item, dict) else item
            path = str(path or "").strip().replace("\\", "/").lstrip("/")
            if path:
                names.append(path)
        return names

    def _looks_static(self, names: list[str]) -> Optional[bool]:
        """按上游文件判断：True=纯静态，False=含后端/构建产物，None=无法判断。"""
        if not names:
            return None
        base = {os.path.basename(name).lower() for name in names}
        build_markers = {
            "package.json", "requirements.txt", "pyproject.toml", "dockerfile",
            "docker-compose.yml", "go.mod", "pom.xml", "cargo.toml", "makefile",
            "manage.py", "app.py",
        }
        if base & build_markers:
            return False
        static_ext = {
            ".html", ".htm", ".css", ".js", ".mjs", ".svg", ".png", ".jpg",
            ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf",
            ".json", ".txt", ".md", ".map",
        }
        exts = {os.path.splitext(name)[1].lower() for name in base}
        if exts and exts <= static_ext:
            return True
        return None

    def _file_present(self, project_dir: str, name: str) -> bool:
        """上游声明的文件是否真的落在项目目录里（支持子目录）。"""
        base = os.path.basename(name)
        for candidate in (os.path.join(project_dir, name), os.path.join(project_dir, base)):
            if os.path.isfile(candidate):
                return True
        hits = glob.glob(os.path.join(project_dir, "**", base), recursive=True)
        return bool(hits)

    def _extract_project_dir(self, message: Message) -> Optional[str]:
        """从消息中提取项目目录

        P1 #11：必须配合 _validate_project_dir 校验，client_dir 来自 LLM 不可信
        """
        # 优先从 metadata 获取
        raw_dir = None
        if "client_dir" in message.metadata:
            raw_dir = message.metadata["client_dir"]

        # 从内容中提取（fallback）
        if not raw_dir:
            content = message.content
            import re
            patterns = [
                r"项目目录[：:]\s*(\S+)",
                r"目录[：:]\s*(\S+)",
                r"client_dir[：:]\s*(\S+)",
            ]
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    raw_dir = match.group(1)
                    break

        if not raw_dir:
            return None

        # P1 #11 修复：必须校验白名单。这里先取 client_name，再用 _validate_project_dir
        client_name = self._extract_client_name(message)
        safe_name = self._generate_service_name(client_name)

        try:
            return self._validate_project_dir(raw_dir, safe_name)
        except ValueError as e:
            logger.error(f"[Security] 拒绝可疑项目目录: {e}")
            return None  # 返回 None 让上层报错

    def _extract_client_name(self, message: Message) -> str:
        """从消息中提取客户名称"""
        # 优先从 metadata 获取
        if "client_name" in message.metadata:
            return message.metadata["client_name"]

        # 从内容中提取
        content = message.content
        import re
        patterns = [
            r"客户[名称]*[：:]\s*(\S+)",
            r"client_name[：:]\s*(\S+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1)

        # 使用时间戳
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _generate_service_name(self, client_name: str) -> str:
        """生成服务名称（用于 URL 路径）"""
        import re
        # 清理名称，只保留字母、数字、连字符
        safe_name = re.sub(r'[^a-zA-Z0-9-]', '-', client_name)
        safe_name = re.sub(r'-+', '-', safe_name)
        safe_name = safe_name.strip('-').lower()

        return safe_name or f"service-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    async def _write_config_files(self, project_dir: str, project_info, service_name: str):
        """写入 Dockerfile 和 docker-compose.yml

        P2 #22 修复：避免覆盖用户已有的配置文件
          - 文件已存在时，先备份（.bak.<timestamp>）再写
          - 在 audit log 记录"覆盖 vs 首次创建"
        """
        import os
        import time

        # 写入 Dockerfile
        dockerfile_path = os.path.join(project_dir, "Dockerfile")
        self._safe_write_file(dockerfile_path, project_info.dockerfile_content, ".bak")

        # 写入 docker-compose.yml
        compose_path = os.path.join(project_dir, "docker-compose.yml")
        compose_content = project_info.compose_content.replace("{{PORT}}", "8000")
        self._safe_write_file(compose_path, compose_content, ".bak")

    @staticmethod
    def _safe_write_file(path: str, content: str, backup_suffix: str = ".bak") -> str:
        """P2 #22：写入文件时先备份已有内容

        Args:
            path: 目标文件路径
            content: 新内容
            backup_suffix: 备份文件后缀

        Returns:
            "created" | "updated" | "updated_with_backup"
        """
        import os
        import time

        if os.path.exists(path):
            # 文件已存在：先备份
            timestamp = int(time.time())
            backup_path = f"{path}{backup_suffix}.{timestamp}"
            try:
                os.rename(path, backup_path)
                logger.info(f"[P2 #22] 备份已存在文件: {path} -> {backup_path}")
                mode = "updated_with_backup"
            except Exception as e:
                logger.warning(f"[P2 #22] 备份失败，直接覆盖: {e}")
                mode = "updated"
        else:
            mode = "created"

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return mode

    def _detect_internal_port(self, project_info) -> int:
        """检测容器内部端口"""
        # 从 Dockerfile 中检测 EXPOSE 端口
        import re
        expose_match = re.search(r'EXPOSE\s+(\d+)', project_info.dockerfile_content)
        if expose_match:
            return int(expose_match.group(1))

        # 根据项目类型返回默认端口
        if project_info.project_type == "static":
            return 80
        elif project_info.project_type == "node":
            return 3000
        elif project_info.project_type == "python":
            return 8000

        return 8000

    def _generate_deploy_report(self, record: DeployRecord) -> str:
        """生成部署报告"""
        return f"""# 部署成功 ✅

## 服务信息
- **服务名称**: {record.service_name}
- **客户名称**: {record.client_name}
- **项目类型**: {record.project_type}
- **技术栈**: {', '.join(record.tech_stack) if record.tech_stack else '未识别'}

## 访问信息
- **URL**: {record.url}
- **端口**: {record.port}
- **容器**: {record.container_name}

## 部署详情
- **镜像**: {record.image_name}
- **健康检查**: {record.health_check_path}
- **监控状态**: 已启动（每 30 秒检查一次）

## 注意事项
- 服务已配置自动重启（unless-stopped）
- 如需停止服务，请联系运维
- 日志可通过 `docker logs {record.container_name}` 查看
"""

    # ========== 监控回调 ==========

    async def _on_service_failure(self, service_name: str, error: str):
        """服务失败时的回调"""
        logger.error(f"服务失败通知: {service_name} - {error}")

        # 通知 CEO
        if self._feishu_client and self._ceo_chat_id:
            try:
                await self._feishu_client.send_text(
                    self._ceo_chat_id,
                    f"⚠️ 服务告警\n\n服务 {service_name} 健康检查失败，已达到最大重启次数。\n错误: {error}\n\n需要人工介入处理。"
                )
            except Exception as e:
                logger.error(f"发送飞书通知失败: {e}")

    async def _on_service_restart(self, service_name: str, restart_count: int):
        """服务重启时的回调"""
        logger.info(f"服务重启: {service_name}（第 {restart_count} 次）")

    async def _on_service_recovered(self, service_name: str):
        """服务恢复时的回调"""
        logger.info(f"服务恢复: {service_name}")

        # 通知 CEO
        if self._feishu_client and self._ceo_chat_id:
            try:
                await self._feishu_client.send_text(
                    self._ceo_chat_id,
                    f"✅ 服务恢复\n\n服务 {service_name} 已自动恢复正常运行。"
                )
            except Exception as e:
                logger.error(f"发送飞书通知失败: {e}")

    # ========== Web 接口预留 ==========


    def get_deployment(self, deployment_id: str, client_id: str = "") -> Optional[DeployRecord]:
        """按 deployment_id 找记录；传了 client_id 时必须匹配。"""
        record = self.deployed_services.get(deployment_id)
        if not record or not client_id or record.client_id != client_id:
            return None
        return record

    def get_all_services(self, client_id: str = "") -> list[dict]:
        """
        获取所有已部署服务的状态（Web 接口用）

        Returns:
            list[dict]: 服务状态列表
        """
        services = []
        for deployment_id, record in self.deployed_services.items():
            if client_id and record.client_id != client_id:
                continue
            services.append({
                "deployment_id": deployment_id,
                "client_id": record.client_id,
                "service_name": record.service_name,
                "client_name": record.client_name,
                "project_type": record.project_type,
                "tech_stack": record.tech_stack,
                "url": record.url,
                "port": record.port,
                "container_name": record.container_name,
                "status": record.status.value,
                "health_status": record.health.status.value,
                "restart_count": record.restart_count,
                "created_at": record.created_at.isoformat(),
                "last_check_at": record.last_check_at.isoformat() if record.last_check_at else None,
                "error_message": record.error_message,
            })
        return services

    def get_service_detail(self, deployment_id: str, client_id: str = "") -> Optional[dict]:
        """
        获取单个服务的详细信息（Web 接口用）

        Args:
            service_name: 服务名称

        Returns:
            Optional[dict]: 服务详细信息
        """
        record = self.get_deployment(deployment_id, client_id)
        if not record:
            return None

        return {
            "deployment_id": deployment_id,
            "service_name": record.service_name,
            "client_name": record.client_name,
            "client_dir": record.client_dir,
            "project_type": record.project_type,
            "tech_stack": record.tech_stack,
            "image_name": record.image_name,
            "container_name": record.container_name,
            "port": record.port,
            "url": record.url,
            "health_check_path": record.health_check_path,
            "status": record.status.value,
            "health_status": record.health.status.value,
            "health_status_code": record.health.status_code,
            "health_response_time_ms": record.health.response_time_ms,
            "health_error": record.health.error,
            "restart_count": record.restart_count,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "stopped_at": record.stopped_at.isoformat() if record.stopped_at else None,
            "last_check_at": record.last_check_at.isoformat() if record.last_check_at else None,
            "last_restart_at": record.last_restart_at.isoformat() if record.last_restart_at else None,
        }
