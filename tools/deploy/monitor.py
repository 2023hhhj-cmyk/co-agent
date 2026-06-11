"""
服务监控器 - 健康检查和持续监控

核心功能：
- HTTP 健康检查
- 容器状态监控
- 异常自动重启
- 告警通知
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Optional

import httpx

from models.deploy import DeployRecord, HealthStatus, ServiceHealth, ServiceStatus
from tools.deploy.docker import DockerManager

logger = logging.getLogger(__name__)


class ServiceMonitor:
    """
    服务监控器

    持续监控已部署的服务，执行健康检查，处理异常。
    """

    def __init__(
        self,
        docker_manager: DockerManager,
        check_interval: int = 30,
        max_restart_attempts: int = 3,
    ):
        """
        初始化监控器

        Args:
            docker_manager: Docker 管理器
            check_interval: 检查间隔（秒）
            max_restart_attempts: 最大重启次数
        """
        self.docker = docker_manager
        self.check_interval = check_interval
        self.max_restart_attempts = max_restart_attempts

        # 已注册的服务
        self.services: dict[str, DeployRecord] = {}  # service_name -> DeployRecord

        # 监控任务
        self._tasks: dict[str, asyncio.Task] = {}  # service_name -> task

        # 回调函数
        self._on_failure: Optional[Callable] = None
        self._on_restart: Optional[Callable] = None
        self._on_recovered: Optional[Callable] = None

    def set_callbacks(
        self,
        on_failure: Optional[Callable] = None,
        on_restart: Optional[Callable] = None,
        on_recovered: Optional[Callable] = None,
    ):
        """
        设置回调函数

        Args:
            on_failure: 服务失败时的回调 (service_name, error)
            on_restart: 服务重启时的回调 (service_name, restart_count)
            on_recovered: 服务恢复时的回调 (service_name)
        """
        self._on_failure = on_failure
        self._on_restart = on_restart
        self._on_recovered = on_recovered

    async def start_monitoring(self, service: DeployRecord):
        """
        开始监控服务

        Args:
            service: 部署记录
        """
        service_name = service.service_name

        # 保存服务信息
        self.services[service_name] = service

        # 如果已有监控任务，先停止
        if service_name in self._tasks:
            self._tasks[service_name].cancel()

        # 启动新的监控任务
        task = asyncio.create_task(
            self._monitor_loop(service_name),
            name=f"monitor-{service_name}",
        )
        self._tasks[service_name] = task

        logger.info(f"开始监控服务: {service_name}")

    async def stop_monitoring(self, service_name: str):
        """
        停止监控服务

        Args:
            service_name: 服务名称
        """
        if service_name in self._tasks:
            self._tasks[service_name].cancel()
            try:
                await self._tasks[service_name]
            except asyncio.CancelledError:
                pass
            del self._tasks[service_name]

        if service_name in self.services:
            del self.services[service_name]

        logger.info(f"停止监控服务: {service_name}")

    async def stop_all(self):
        """停止所有监控"""
        for service_name in list(self._tasks.keys()):
            await self.stop_monitoring(service_name)

        logger.info("停止所有监控")

    async def check_health(self, service: DeployRecord) -> ServiceHealth:
        """
        执行健康检查

        Args:
            service: 部署记录

        Returns:
            ServiceHealth: 健康检查结果
        """
        # 1. 检查容器是否在运行
        is_running = await self.docker.is_running(service.container_name)
        if not is_running:
            return ServiceHealth(
                status=HealthStatus.UNHEALTHY,
                error="容器未运行",
            )

        # 2. HTTP 健康检查
        url = f"http://127.0.0.1:{service.port}{service.health_check_path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                start_time = datetime.now()
                response = await client.get(url)
                end_time = datetime.now()

                response_time_ms = (end_time - start_time).total_seconds() * 1000

                if response.status_code < 400:
                    return ServiceHealth(
                        status=HealthStatus.HEALTHY,
                        status_code=response.status_code,
                        response_time_ms=response_time_ms,
                    )
                else:
                    return ServiceHealth(
                        status=HealthStatus.UNHEALTHY,
                        status_code=response.status_code,
                        error=f"HTTP 状态码: {response.status_code}",
                    )
        except httpx.TimeoutException:
            return ServiceHealth(
                status=HealthStatus.UNHEALTHY,
                error="请求超时",
            )
        except Exception as e:
            return ServiceHealth(
                status=HealthStatus.UNHEALTHY,
                error=str(e),
            )

    async def _monitor_loop(self, service_name: str):
        """
        监控循环

        Args:
            service_name: 服务名称
        """
        logger.info(f"监控循环启动: {service_name}")

        while service_name in self.services:
            try:
                service = self.services[service_name]

                # 执行健康检查
                health = await self.check_health(service)
                service.update_health(health)

                # 处理不健康的情况
                if health.status == HealthStatus.UNHEALTHY:
                    logger.warning(f"服务 {service_name} 不健康: {health.error}")
                    await self._handle_unhealthy(service_name)

                # 等待下一次检查
                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                logger.info(f"监控循环取消: {service_name}")
                break
            except Exception as e:
                logger.error(f"监控循环异常: {service_name} - {e}")
                await asyncio.sleep(self.check_interval)

        logger.info(f"监控循环结束: {service_name}")

    async def _handle_unhealthy(self, service_name: str):
        """
        处理不健康的服务

        Args:
            service_name: 服务名称
        """
        service = self.services.get(service_name)
        if not service:
            return

        # 检查重启次数
        if service.restart_count >= self.max_restart_attempts:
            logger.error(f"服务 {service_name} 已达到最大重启次数，标记为失败")
            service.mark_failed(f"健康检查失败，已重启 {service.restart_count} 次")

            # 调用失败回调
            if self._on_failure:
                await self._on_failure(service_name, service.error_message)
            return

        # 尝试重启
        logger.info(f"尝试重启服务 {service_name}（第 {service.restart_count + 1} 次）")
        service.mark_restarting()

        # 调用重启回调
        if self._on_restart:
            await self._on_restart(service_name, service.restart_count)

        # 执行重启
        success = await self.docker.restart(service.container_name)

        if success:
            # 等待服务启动
            await asyncio.sleep(5)

            # 再次检查健康状态
            health = await self.check_health(service)
            service.update_health(health)

            if health.status == HealthStatus.HEALTHY:
                logger.info(f"服务 {service_name} 重启成功")
                service.mark_running()

                # 调用恢复回调
                if self._on_recovered:
                    await self._on_recovered(service_name)
            else:
                logger.warning(f"服务 {service_name} 重启后仍然不健康")
        else:
            logger.error(f"服务 {service_name} 重启失败")

    async def get_all_status(self) -> list[dict]:
        """
        获取所有服务的状态

        Returns:
            list[dict]: 服务状态列表
        """
        statuses = []
        for service_name, service in self.services.items():
            statuses.append({
                "service_name": service_name,
                "status": service.status.value,
                "health": service.health.status.value,
                "url": service.url,
                "port": service.port,
                "restart_count": service.restart_count,
                "last_check": service.last_check_at.isoformat() if service.last_check_at else None,
                "error": service.error_message,
            })
        return statuses
