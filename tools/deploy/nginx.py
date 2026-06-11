"""
Nginx 配置管理器 - 管理反向代理配置

核心功能：
- 生成 location 配置块
- 更新主配置文件
- 重载 Nginx 配置
- 删除 location 配置
"""

import asyncio
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Nginx 配置文件路径
NGINX_CONF_DIR = "/etc/nginx/conf.d"
NGINX_SITES_AVAILABLE = "/etc/nginx/sites-available"
NGINX_SITES_ENABLED = "/etc/nginx/sites-enabled"

# 默认配置文件名
DEFAULT_CONF_NAME = "loveboos-ai.conf"


class NginxManager:
    """
    Nginx 配置管理器

    管理 loveboos-ai.com 的反向代理配置。
    使用路径方式：http://loveboos-ai.com/{service_name}
    """

    def __init__(
        self,
        domain: str = "loveboos-ai.com",
        conf_dir: str = NGINX_CONF_DIR,
    ):
        self.domain = domain
        self.conf_dir = conf_dir
        self.conf_name = DEFAULT_CONF_NAME
        self.conf_path = os.path.join(conf_dir, self.conf_name)

    def generate_location(self, service_name: str, port: int) -> str:
        """
        生成 location 配置块

        Args:
            service_name: 服务名称（用于 URL 路径）
            port: 内部端口

        Returns:
            str: Nginx location 配置块
        """
        return f"""
    # {service_name}
    location /{service_name}/ {{
        proxy_pass http://127.0.0.1:{port}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}"""

    async def add_service(self, service_name: str, port: int) -> bool:
        """
        添加服务的反向代理配置

        Args:
            service_name: 服务名称
            port: 内部端口

        Returns:
            bool: 是否成功
        """
        logger.info(f"添加 Nginx 配置: {service_name} -> port {port}")

        # 读取现有配置
        config = await self._read_config()
        if config is None:
            # 配置文件不存在，创建新的
            config = self._generate_base_config()

        # 检查是否已存在该服务的配置
        if f"location /{service_name}/" in config:
            logger.info(f"服务 {service_name} 的配置已存在，先删除")
            config = await self._remove_location_config(config, service_name)

        # 添加新的 location 配置
        location_config = self.generate_location(service_name, port)

        # 在最后一个 } 之前插入
        insert_pos = config.rstrip().rfind("}")
        if insert_pos != -1:
            config = config[:insert_pos] + location_config + "\n}"

        # 写入配置文件
        success = await self._write_config(config)
        if success:
            logger.info(f"Nginx 配置添加成功: {service_name}")
        else:
            logger.error(f"Nginx 配置添加失败: {service_name}")

        return success

    async def remove_service(self, service_name: str) -> bool:
        """
        删除服务的反向代理配置

        Args:
            service_name: 服务名称

        Returns:
            bool: 是否成功
        """
        logger.info(f"删除 Nginx 配置: {service_name}")

        config = await self._read_config()
        if config is None:
            logger.warning("Nginx 配置文件不存在")
            return True

        # 删除 location 配置
        new_config = await self._remove_location_config(config, service_name)

        # 写入配置文件
        success = await self._write_config(new_config)
        if success:
            logger.info(f"Nginx 配置删除成功: {service_name}")
        else:
            logger.error(f"Nginx 配置删除失败: {service_name}")

        return success

    async def reload(self) -> bool:
        """
        重载 Nginx 配置

        Returns:
            bool: 是否成功
        """
        logger.info("重载 Nginx 配置")

        # 先测试配置
        test_ok = await self._test_config()
        if not test_ok:
            logger.error("Nginx 配置测试失败，不重载")
            return False

        # 重载配置
        cmd = ["nginx", "-s", "reload"]
        result = await self._run_command(cmd)

        if result.returncode == 0:
            logger.info("Nginx 重载成功")
            return True
        else:
            logger.error(f"Nginx 重载失败: {result.stderr}")
            return False

    async def get_all_services(self) -> list[dict]:
        """
        获取所有已配置的服务

        Returns:
            list[dict]: 服务列表，包含 service_name 和 port
        """
        config = await self._read_config()
        if config is None:
            return []

        services = []
        # 匹配 location /xxx/ { ... proxy_pass http://127.0.0.1:xxxx/; ... }
        pattern = r'location\s+/(\w+)/\s*\{[^}]*proxy_pass\s+http://127\.0\.0\.1:(\d+)/'
        matches = re.finditer(pattern, config)

        for match in matches:
            services.append({
                "service_name": match.group(1),
                "port": int(match.group(2)),
                "url": f"http://{self.domain}/{match.group(1)}",
            })

        return services

    async def check_nginx_installed(self) -> bool:
        """检查 Nginx 是否已安装"""
        cmd = ["which", "nginx"]
        result = await self._run_command(cmd)
        return result.returncode == 0

    async def install_nginx(self) -> bool:
        """安装 Nginx"""
        logger.info("安装 Nginx")

        # 更新包列表
        cmd = ["apt-get", "update"]
        await self._run_command(cmd)

        # 安装 Nginx
        cmd = ["apt-get", "install", "-y", "nginx"]
        result = await self._run_command(cmd)

        if result.returncode == 0:
            logger.info("Nginx 安装成功")

            # 启动 Nginx
            cmd = ["systemctl", "start", "nginx"]
            await self._run_command(cmd)

            # 设置开机自启
            cmd = ["systemctl", "enable", "nginx"]
            await self._run_command(cmd)

            return True
        else:
            logger.error(f"Nginx 安装失败: {result.stderr}")
            return False

    def _generate_base_config(self) -> str:
        """生成基础配置文件"""
        return f"""server {{
    listen 80;
    server_name {self.domain};

    # 健康检查
    location /health {{
        return 200 'OK';
        add_header Content-Type text/plain;
    }}

    # 服务配置将自动添加到此处
}}"""

    async def _read_config(self) -> Optional[str]:
        """读取配置文件"""
        try:
            if os.path.exists(self.conf_path):
                with open(self.conf_path, 'r', encoding='utf-8') as f:
                    return f.read()
            return None
        except Exception as e:
            logger.error(f"读取 Nginx 配置失败: {e}")
            return None

    async def _write_config(self, config: str) -> bool:
        """写入配置文件"""
        try:
            # 确保目录存在
            os.makedirs(self.conf_dir, exist_ok=True)

            with open(self.conf_path, 'w', encoding='utf-8') as f:
                f.write(config)

            return True
        except Exception as e:
            logger.error(f"写入 Nginx 配置失败: {e}")
            return False

    async def _remove_location_config(self, config: str, service_name: str) -> str:
        """从配置中删除指定服务的 location 块"""
        # 匹配 location /xxx/ { ... } 块（包括注释）
        pattern = rf'\n\s*# {service_name}\n\s*location /{service_name}/ \{{[^}}]*\}}'
        return re.sub(pattern, '', config)

    async def _test_config(self) -> bool:
        """测试 Nginx 配置"""
        cmd = ["nginx", "-t"]
        result = await self._run_command(cmd)
        return result.returncode == 0

    async def _run_command(self, cmd: list[str]):
        """执行命令"""
        logger.debug(f"执行命令: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        return type('Result', (), {
            'returncode': process.returncode,
            'stdout': stdout.decode('utf-8', errors='replace').strip(),
            'stderr': stderr.decode('utf-8', errors='replace').strip(),
        })()
