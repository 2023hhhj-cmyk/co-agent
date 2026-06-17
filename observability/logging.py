"""
结构化日志 — JSON 格式 + trace_id 全链路追踪

每条日志输出为 JSON，包含：
- timestamp, level, module
- trace_id: 从 API 请求到 Agent 执行全链路一致
- agent_id: 当前 Agent
- extra: 额外上下文字段

使用方式：
  from observability.logging import setup_logging, get_trace_id, set_trace_id

  # 在 main.py 启动时调用
  setup_logging()

  # 在 API 入口设置 trace_id
  trace_id = str(uuid.uuid4())
  set_trace_id(trace_id)
  logger.info("请求开始", extra={"endpoint": "/api/requirement"})
"""

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ContextVar 用于跨协程传递 trace_id（无需修改函数签名）
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
_agent_id: ContextVar[str] = ContextVar("agent_id", default="")


def get_trace_id() -> str:
    """获取当前协程的 trace_id"""
    return _trace_id.get()


def set_trace_id(trace_id: str) -> None:
    """设置当前协程的 trace_id"""
    _trace_id.set(trace_id)


def generate_trace_id() -> str:
    """生成新的 trace_id"""
    tid = f"tr-{uuid.uuid4().hex[:12]}"
    _trace_id.set(tid)
    return tid


def get_agent_id() -> str:
    return _agent_id.get()


def set_agent_id(agent_id: str) -> None:
    _agent_id.set(agent_id)


class JSONFormatter(logging.Formatter):
    """JSON 格式日志格式化器"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # 注入 trace_id
        tid = _trace_id.get()
        if tid:
            log_entry["trace_id"] = tid

        # 注入 agent_id
        aid = _agent_id.get()
        if aid:
            log_entry["agent_id"] = aid[:8]

        # 注入额外字段（通过 extra={"key": val} 传入）
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)

        # 异常信息
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class AgentLogger:
    """带 agent_id 和 trace_id 的 Logger 包装"""

    def __init__(self, name: str, agent_id: str = ""):
        self._logger = logging.getLogger(name)
        self.agent_id = agent_id

    def _log(self, level: int, msg: str, **extra):
        set_agent_id(self.agent_id)
        extra_fields = extra if extra else {}
        self._logger.log(level, msg, extra={"extra_fields": extra_fields} if extra_fields else {})

    def info(self, msg: str, **extra):
        self._log(logging.INFO, msg, **extra)

    def warning(self, msg: str, **extra):
        self._log(logging.WARNING, msg, **extra)

    def error(self, msg: str, **extra):
        self._log(logging.ERROR, msg, **extra)

    def debug(self, msg: str, **extra):
        self._log(logging.DEBUG, msg, **extra)


def setup_logging(level: int = logging.INFO):
    """
    初始化结构化日志

    在 main.py 启动时调用，替换默认的文本格式日志。

    Args:
        level: 日志级别
    """
    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler
    root.handlers.clear()

    # JSON handler
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    # 第三方库保持 WARNING
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)

    root.info("结构化日志已初始化 (JSON format)")
