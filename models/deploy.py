"""
部署记录数据模型 - DevOps Agent 的服务管理

核心设计：
- ServiceStatus: 服务状态枚举
- DeployRecord: 部署记录，包含服务的所有信息
- ServiceHealth: 健康检查结果
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ServiceStatus(str, Enum):
    """服务状态"""
    DEPLOYING = "deploying"      # 部署中
    RUNNING = "running"          # 运行中
    STOPPED = "stopped"          # 已停止
    FAILED = "failed"            # 失败
    RESTARTING = "restarting"    # 重启中
    MAINTENANCE = "maintenance"  # 维护中


class HealthStatus(str, Enum):
    """健康状态"""
    HEALTHY = "healthy"          # 健康
    UNHEALTHY = "unhealthy"      # 不健康
    UNKNOWN = "unknown"          # 未知


class ServiceHealth(BaseModel):
    """健康检查结果"""
    status: HealthStatus = HealthStatus.UNKNOWN
    status_code: Optional[int] = None      # HTTP 状态码
    response_time_ms: Optional[float] = None  # 响应时间（毫秒）
    error: Optional[str] = None            # 错误信息
    checked_at: datetime = Field(default_factory=datetime.now)


class DeployRecord(BaseModel):
    """部署记录"""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    service_name: str          # 服务名称（用于 URL 路径）
    client_name: str           # 客户名称
    client_id: str = ""       # 固定客户编号
    client_dir: str            # 项目目录
    project_type: str = "unknown"  # 项目类型 (node/python/static/mixed)
    tech_stack: list[str] = Field(default_factory=list)  # 技术栈

    # Docker 配置
    image_name: str = ""       # Docker 镜像名
    container_name: str = ""   # 容器名
    port: int = 0              # 分配的端口

    # 访问配置
    url: str = ""              # 访问 URL（如 {PUBLIC_BASE_URL}/client-a）
    health_check_path: str = "/"  # 健康检查路径

    # 状态
    status: ServiceStatus = ServiceStatus.DEPLOYING
    health: ServiceHealth = Field(default_factory=ServiceHealth)
    error_message: Optional[str] = None

    # 监控信息
    restart_count: int = 0     # 重启次数
    last_restart_at: Optional[datetime] = None
    last_check_at: Optional[datetime] = None

    # 时间戳
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    stopped_at: Optional[datetime] = None

    def mark_running(self):
        """标记为运行中"""
        self.status = ServiceStatus.RUNNING
        self.updated_at = datetime.now()

    def mark_failed(self, error: str):
        """标记为失败"""
        self.status = ServiceStatus.FAILED
        self.error_message = error
        self.updated_at = datetime.now()

    def mark_stopped(self):
        """标记为已停止"""
        self.status = ServiceStatus.STOPPED
        self.stopped_at = datetime.now()
        self.updated_at = datetime.now()

    def mark_restarting(self):
        """标记为重启中"""
        self.status = ServiceStatus.RESTARTING
        self.restart_count += 1
        self.last_restart_at = datetime.now()
        self.updated_at = datetime.now()

    def update_health(self, health: ServiceHealth):
        """更新健康状态"""
        self.health = health
        self.last_check_at = datetime.now()
        self.updated_at = datetime.now()

    def to_summary(self) -> str:
        """生成简短的状态摘要"""
        status_icon = {
            ServiceStatus.DEPLOYING: "🔄",
            ServiceStatus.RUNNING: "✅",
            ServiceStatus.STOPPED: "⏹️",
            ServiceStatus.FAILED: "❌",
            ServiceStatus.RESTARTING: "🔄",
            ServiceStatus.MAINTENANCE: "🔧",
        }
        icon = status_icon.get(self.status, "❓")
        health_icon = "💚" if self.health.status == HealthStatus.HEALTHY else "❤️"
        return f"{icon} {self.service_name} ({self.url}) {health_icon}"


class PortAllocation(BaseModel):
    """端口分配记录"""
    port: int
    service_name: str
    allocated_at: datetime = Field(default_factory=datetime.now)
