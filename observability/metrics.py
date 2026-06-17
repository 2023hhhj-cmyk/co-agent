"""
Prometheus 指标定义与注册

指标命名规范：muzhu_{category}_{name}_{unit}

暴露端点：GET /metrics (text/plain; version=0.0.4)

使用方式：
  from observability.metrics import (
      agent_state, llm_call_duration, messages_processed,
      task_duration, input_rejected, sandbox_mode,
  )

  agent_state.labels(agent="ceo").set(1)
"""

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CollectorRegistry

# 使用独立 Registry，仅暴露应用指标，不泄露 Python 进程内部信息
_CUSTOM_REGISTRY = CollectorRegistry(auto_describe=False)

# ========== Agent 状态 ==========

agent_state = Gauge(
    "muzhu_agent_state",
    "Agent current state (0=idle, 1=busy, 2=error)",
    ["agent", "role"],
    registry=_CUSTOM_REGISTRY,
)

# ========== LLM 调用 ==========

llm_call_duration = Histogram(
    "muzhu_llm_call_duration_seconds",
    "LLM API call duration in seconds",
    ["model", "status"],
    buckets=[1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0],
    registry=_CUSTOM_REGISTRY,
)

llm_call_total = Counter(
    "muzhu_llm_call_total",
    "Total LLM API calls",
    ["model", "status"],
    registry=_CUSTOM_REGISTRY,
)

# ========== 消息处理 ==========

messages_processed = Counter(
    "muzhu_messages_processed_total",
    "Total messages processed",
    ["agent", "cause_by"],
    registry=_CUSTOM_REGISTRY,
)

# ========== 任务执行 ==========

task_duration = Histogram(
    "muzhu_task_duration_seconds",
    "Task execution duration in seconds",
    ["agent"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
    registry=_CUSTOM_REGISTRY,
)

task_total = Counter(
    "muzhu_task_total",
    "Total tasks executed",
    ["agent", "status"],
    registry=_CUSTOM_REGISTRY,
)

# ========== 记忆操作 ==========

memory_operations = Counter(
    "muzhu_memory_operations_total",
    "Total memory operations",
    ["operation"],  # recall, remember, archive, consolidate
    registry=_CUSTOM_REGISTRY,
)

# ========== 安全 ==========

input_rejected = Counter(
    "muzhu_input_rejected_total",
    "Total inputs rejected by InputGuard",
    ["source", "reason"],
    registry=_CUSTOM_REGISTRY,
)

sandbox_mode = Gauge(
    "muzhu_sandbox_mode",
    "Sandbox execution mode (1=docker, 0=fallback_readonly)",
    registry=_CUSTOM_REGISTRY,
)

# ========== 队列 ==========

queue_depth = Gauge(
    "muzhu_queue_depth",
    "Customer service queue depth",
    ["queue_name"],
    registry=_CUSTOM_REGISTRY,
)

# ========== 缓存 ==========

llm_cache_hit = Counter(
    "muzhu_llm_cache_hit_total",
    "Total LLM cache hits",
    ["model"],
    registry=_CUSTOM_REGISTRY,
)

llm_cache_miss = Counter(
    "muzhu_llm_cache_miss_total",
    "Total LLM cache misses",
    ["model"],
    registry=_CUSTOM_REGISTRY,
)

# ========== HTTP ==========

http_requests = Counter(
    "muzhu_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
    registry=_CUSTOM_REGISTRY,
)

http_request_duration = Histogram(
    "muzhu_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    registry=_CUSTOM_REGISTRY,
)


def get_metrics() -> bytes:
    """生成 Prometheus 格式的指标数据（仅应用指标，不含 Python 进程内部指标）"""
    return generate_latest(_CUSTOM_REGISTRY)
