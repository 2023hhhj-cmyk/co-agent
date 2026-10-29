"""
Analytics 埋点助手 — 采集真实可量化的运行数据。

设计原则：
- 失败隔离：emit_event 内 try/except 吞掉所有异常，绝不阻塞主流程。
- 进程内并发安全：单线程 asyncio + threading.Lock 即可。
- 跨进程限制：多进程部署会丢行（详见 analytics_spec.md 第 6 节）。
- 隐私优先：调用方传入的字段值应预先脱敏（hash_id / hash_args），
  本模块不做任何猜测。

使用示例：
    from analytics import emit_event, hash_id, hash_args

    emit_event(
        "feishu.message.received",
        user_id_hash=hash_id(sender_id),
        msg_len=len(text),
        intent=intent_label,
    )
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ========== 文件路径 ==========
# 项目根目录下的 analytics_events.log（NDJSON，一行一个事件）
_LOG_PATH = Path(os.environ.get("ANALYTICS_LOG_PATH", "analytics_events.log")).resolve()

# ========== trace_id 上下文 ==========
# 优先用 observability.logging 的 trace_id；如果该模块未初始化则本地备一个。
_event_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "analytics_trace_id", default=""
)

try:
    from observability.logging import get_trace_id as _get_obs_trace_id  # type: ignore
except Exception:  # noqa: BLE001
    _get_obs_trace_id = None  # type: ignore[assignment]


def _resolve_trace_id() -> str:
    """优先取 observability 的 trace_id，其次取本模块上下文，最后临时生成。"""
    if _get_obs_trace_id is not None:
        try:
            tid = _get_obs_trace_id()
            if tid:
                return tid
        except Exception:  # noqa: BLE001
            pass
    tid = _event_trace_id.get()
    if tid:
        return tid
    return f"tr-{uuid.uuid4().hex[:12]}"


def set_event_trace_id(tid: str) -> None:
    """显式为本事件设置 trace_id（用于跨模块串联）。"""
    _event_trace_id.set(tid)


# ========== 文件写入 ==========
# 单进程内用 Lock 串行化；多进程下应改为 queue + 单写者。
_write_lock = threading.Lock()


def _ensure_parent_dir() -> None:
    parent = _LOG_PATH.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _write_line(line: str) -> None:
    """追加写入一行。失败绝不抛出。"""
    try:
        _ensure_parent_dir()
        with _write_lock:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        # 仅 DEBUG 输出，避免污染 stderr
        logger.debug("analytics: 写入失败 %s", exc)


# ========== 公共 API ==========


def emit_event(event_name: str, **fields: Any) -> None:
    """
    发射一个埋点事件。

    Args:
        event_name: 事件名（见 analytics_spec.md 定义）
        **fields: 事件属性（必须可 JSON 序列化；不可序列化的值会被 str()）

    任何异常都会被吞掉，调用方无须保护。
    """
    try:
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_name,
            "trace_id": _resolve_trace_id(),
        }
        for k, v in fields.items():
            try:
                json.dumps(v, ensure_ascii=False)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = str(v)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        _write_line(line)
        # ponytail: stderr mirror, off by default. ANALYTICS_VERBOSE=1 to enable.
        if os.environ.get("ANALYTICS_VERBOSE") == "1":
            try:
                print(line, file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("analytics: emit 失败 %s", exc)


def hash_id(raw: str) -> str:
    """对敏感 ID 做不可逆哈希（取 SHA256 前 16 hex）。空值返回空串。"""
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def hash_args(args: Any) -> str:
    """对动作参数做去重哈希。"""
    try:
        s = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(args)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def get_log_path() -> Path:
    """返回当前日志文件路径（供测试和文档引用）。"""
    return _LOG_PATH
