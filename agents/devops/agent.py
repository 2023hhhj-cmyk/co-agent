"""
运维工程师 Agent - 重写版本

职责：
1. 接收 Developer 的产出，自动部署应用
2. 使用 Claude Code 分析项目，生成 Dockerfile
3. Docker 容器化部署
4. Nginx 反向代理配置
5. 持续监控和异常自愈
6. Web 管理接口预留

工作流程：
- 收到部署请求 → 分析项目 → 构建镜像 → 启动容器 → 配置 Nginx → 健康检查 → 监控
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

from agents.base import BaseAgent
from models.agent import ActionDef, AgentRole, ReactMode
from models.deploy import DeployRecord, ServiceStatus
from models.message import Message
from tools.code.claude_code import ClaudeCodeTool
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
    tools=["docker", "nginx", "monitoring", "claude_code"],
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
    watch={"review_code", "deploy_request", "deploy_fix"},
    react_mode=ReactMode.REACT,
    max_react_loop=3,
)


class DevOpsAgent(BaseAgent):
    """运维工程师 Agent"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        message_bus: Any = None,
        base_dir: str = "/root",
    ):
        super().__init__(
            role=DEVOPS_ROLE,
            llm_client=llm_client,
            message_bus=message_bus,
        )

        self.base_dir = base_dir

        # 初始化工具
        self.claude_code = ClaudeCodeTool(
            base_dir=base_dir,
            timeout=300,
        )
        self.analyzer = ProjectAnalyzer(self.claude_code)
        self.port_manager = PortManager(start=8001, end=9000)
        self.docker = DockerManager(self.port_manager)
        self.nginx = NginxManager(domain="loveboos-ai.com")
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

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """思考阶段"""
        if not messages:
            return None

        last_message = messages[-1]

        if last_message.cause_by in ("review_code", "deploy_request"):
            return self.role.get_action_by_name("deploy")
        elif last_message.cause_by == "deploy_fix":
            return self.role.get_action_by_name("fix")

        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """执行阶段"""
        messages = context.get("messages", []) if context else []
        last_message = messages[-1] if messages else None

        if action.name == "deploy":
            return await self._deploy(last_message)
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

        # 解析项目信息
        project_dir = self._extract_project_dir(last_message)
        client_name = self._extract_client_name(last_message)

        if not project_dir:
            return Message(
                content="无法解析项目目录",
                cause_by="error",
                sent_from=self.agent_id,
            )

        # 生成服务名称
        service_name = self._generate_service_name(client_name)

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

            # 第 2 步：生成配置文件（如果 Claude Code 没有生成）
            if not project_info.dockerfile_content:
                logger.info("Claude Code 未生成 Dockerfile，使用默认配置")
                project_info.dockerfile_content = self.analyzer.generate_default_dockerfile(
                    project_info.project_type
                )

            if not project_info.compose_content:
                logger.info("Claude Code 未生成 docker-compose.yml，使用默认配置")
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
            port = self.port_manager.allocate(service_name)
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
            self.deployed_services[service_name] = record
            await self.monitor.start_monitoring(record)

            # 生成访问 URL
            url = f"http://loveboos-ai.com/{service_name}"
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
                },
            )

        except Exception as e:
            logger.error(f"部署失败: {service_name} - {e}")
            record.mark_failed(str(e))
            self.deployed_services[service_name] = record

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

        service_name = last_message.metadata.get("service_name")
        if not service_name or service_name not in self.deployed_services:
            return Message(
                content=f"未找到服务: {service_name}",
                cause_by="error",
                sent_from=self.agent_id,
            )

        service = self.deployed_services[service_name]
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

    def _extract_project_dir(self, message: Message) -> Optional[str]:
        """从消息中提取项目目录"""
        # 优先从 metadata 获取
        if "client_dir" in message.metadata:
            return message.metadata["client_dir"]

        # 从内容中提取
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
                return match.group(1)

        return None

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
        """写入 Dockerfile 和 docker-compose.yml"""
        import os

        # 写入 Dockerfile
        dockerfile_path = os.path.join(project_dir, "Dockerfile")
        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(project_info.dockerfile_content)

        # 写入 docker-compose.yml
        compose_path = os.path.join(project_dir, "docker-compose.yml")
        compose_content = project_info.compose_content.replace("{{PORT}}", "8000")
        with open(compose_path, 'w', encoding='utf-8') as f:
            f.write(compose_content)

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

    def get_all_services(self) -> list[dict]:
        """
        获取所有已部署服务的状态（Web 接口用）

        Returns:
            list[dict]: 服务状态列表
        """
        services = []
        for service_name, record in self.deployed_services.items():
            services.append({
                "service_name": service_name,
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

    def get_service_detail(self, service_name: str) -> Optional[dict]:
        """
        获取单个服务的详细信息（Web 接口用）

        Args:
            service_name: 服务名称

        Returns:
            Optional[dict]: 服务详细信息
        """
        record = self.deployed_services.get(service_name)
        if not record:
            return None

        return {
            "service_name": service_name,
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
