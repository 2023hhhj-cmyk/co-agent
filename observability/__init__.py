"""
可观测性系统 — Metrics + Logging + Health + Alerts

┌──────────────────────────────────────────┐
│  GET /metrics    Prometheus 指标端点      │
│  GET /health     增强健康检查             │
│  JSON logging    结构化日志 + trace_id    │
│  alerts.yml      Prometheus 告警规则      │
└──────────────────────────────────────────┘
"""

from observability.logging import (
    setup_logging,
    get_trace_id,
    set_trace_id,
    generate_trace_id,
    JSONFormatter,
    AgentLogger,
)
from observability.metrics import (
    agent_state,
    llm_call_duration,
    llm_call_total,
    messages_processed,
    task_duration,
    task_total,
    memory_operations,
    input_rejected,
    sandbox_mode,
    queue_depth,
    http_requests,
    http_request_duration,
    get_metrics,
)
from observability.health import HealthChecker

__all__ = [
    "setup_logging",
    "get_trace_id",
    "set_trace_id",
    "generate_trace_id",
    "JSONFormatter",
    "AgentLogger",
    "agent_state",
    "llm_call_duration",
    "llm_call_total",
    "messages_processed",
    "task_duration",
    "task_total",
    "memory_operations",
    "input_rejected",
    "sandbox_mode",
    "queue_depth",
    "http_requests",
    "http_request_duration",
    "get_metrics",
    "HealthChecker",
]
