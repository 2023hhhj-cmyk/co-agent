"""
配置设置
"""
import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)  # ponytail: 忽略未声明 env vars（opencode 的 OPENCODE_* 不污染 Settings）
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DATABASE_URL: str = ""
    REDIS_URL: str = ""

    # 安全配置
    API_KEY: str = ""                           # API 认证密钥（空=开发模式，不校验）
    RATE_LIMIT: str = "60/minute"               # 全局限速
    SSL_CERT_FILE: str = ""                     # TLS 证书路径
    SSL_KEY_FILE: str = ""                      # TLS 私钥路径
    DASHSCOPE_API_KEY: str = Field(default="", repr=False)
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_EMBEDDING_MODEL: str = "text-embedding-v4"
    DASHSCOPE_EMBEDDING_DIMENSIONS: int = 1024
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-v4.1-flash"
    # Docker 中的 opencode 仅接收这些模型配置，不转发平台其他凭证。
    OC_SANDBOX_IMAGE: str = "muzhu-sandbox:latest"
    OC_SANDBOX_MEMORY: str = "1536m"
    OC_SANDBOX_CPUS: float = Field(default=1.0, gt=0)
    OC_TASK_TIMEOUT: int = 1500
    # 对外访问基址：部署消息、Webhook 健康检查都用它，避免硬编码域名
    PUBLIC_BASE_URL: str = "https://www.loveboos-ai.com"
    OPENCODE_MODEL: str = "opencode-go/deepseek-v4-flash"
    OPENCODE_AGENT: str = "build"
    OPENCODE_API_KEY: str = Field(default="", repr=False)  # opencode-go provider key (oc_sk_...)
    # 旧版 Anthropic API 客户端（ClaudeClient）仍在使用，保留该字段。
    ANTHROPIC_API_KEY: str = Field(default="", repr=False)
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_VERIFICATION_TOKEN: str = ""
    FEISHU_ENCRYPT_KEY: str = ""

    # 企业微信 CEO 应用
    WECOM_CEO_CORP_ID: str = ""
    WECOM_CEO_AGENT_ID: str = ""
    WECOM_CEO_SECRET: str = ""
    WECOM_CEO_TOKEN: str = ""          # 回调验证 Token
    WECOM_CEO_AES_KEY: str = ""        # 回调加密 Key

    # 企业微信客服应用
    WECOM_CS_CORP_ID: str = ""
    WECOM_CS_AGENT_ID: str = ""
    WECOM_CS_SECRET: str = ""
    WECOM_CS_TOKEN: str = ""
    WECOM_CS_AES_KEY: str = ""

    # 单进程工作区共享配额（修改后重启服务）
    WORKSPACE_MAX_CONCURRENT_TASKS: int = Field(default=5, ge=1)
    LLM_RPM_LIMIT: int = Field(default=60, ge=1)
    LLM_TPM_LIMIT: int = Field(default=60000, ge=1)
    LLM_MAX_RETRIES: int = Field(default=3, ge=0, le=10)
    LLM_RETRY_BASE_DELAY: float = Field(default=1.0, gt=0)
    LLM_RETRY_MAX_DELAY: float = Field(default=30.0, gt=0)

    # 客服队列配置
    CS_QUEUE_BATCH_SIZE: int = 5       # 每批处理数
    CS_QUEUE_INTERVAL: float = 1.0     # 批次间隔（秒）

    # ponytail: 配置走 model_config（旧的 class Config 已合并到 model_config 上面）

settings = Settings()
