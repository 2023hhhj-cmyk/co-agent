"""
配置设置
"""
import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    DATABASE_URL: str = "postgresql://muzhu:muzhu_pg_2024@localhost:5432/muzhu_agent"
    REDIS_URL: str = "redis://:muzhu_redis_2024@localhost:6379/0"
    CHROMADB_URL: str = "http://localhost:8000"
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-v4-pro"
    ANTHROPIC_API_KEY: str = ""
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

    # 客服队列配置
    CS_QUEUE_BATCH_SIZE: int = 5       # 每批处理数
    CS_QUEUE_INTERVAL: float = 1.0     # 批次间隔（秒）

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
