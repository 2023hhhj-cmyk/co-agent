"""
Layer 1: 短期记忆 (Working Memory)

基于 collections.deque 的内存工作区，用于当前任务的上下文快速访问。
- 容量上限 20 条，FIFO 淘汰
- 任务开始时 clear()，任务结束时 to_dict_list() 归档
"""

import logging
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.message import Message

logger = logging.getLogger(__name__)

# 默认容量
DEFAULT_CAPACITY = 20


class WorkingMemory:
    """短期工作记忆 — 内存 deque 封装"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._buffer: deque[Message] = deque(maxlen=capacity)
        self.capacity = capacity

    # ========== 写入 ==========

    def add(self, message: Message) -> None:
        """添加一条消息到工作记忆（自动淘汰旧消息）"""
        self._buffer.append(message)

    def add_batch(self, messages: List[Message]) -> None:
        """批量添加消息"""
        for msg in messages:
            self._buffer.append(msg)

    # ========== 读取 ==========

    def get_recent(self, k: int = 10) -> List[Message]:
        """获取最近 k 条消息"""
        k = min(k, len(self._buffer))
        if k == 0:
            return []
        return list(self._buffer)[-k:]

    def get_all(self) -> List[Message]:
        """获取全部消息"""
        return list(self._buffer)

    def get_text(self, k: int = 10) -> str:
        """获取最近 k 条消息的文本形式（用于拼接 LLM prompt）"""
        messages = self.get_recent(k)
        lines = []
        for msg in messages:
            sender = getattr(msg, 'sent_from', 'unknown')
            content = getattr(msg, 'content', str(msg))
            lines.append(f"[{sender}]: {content}")
        return "\n".join(lines)

    # ========== 生命周期 ==========

    def clear(self) -> None:
        """清空工作记忆（任务开始时调用）"""
        self._buffer.clear()

    def to_dict_list(self) -> List[Dict[str, Any]]:
        """导出全部记忆为 dict 列表，用于归档到 Layer 2"""
        result = []
        for msg in self._buffer:
            result.append({
                "content": getattr(msg, 'content', str(msg)),
                "sent_from": getattr(msg, 'sent_from', ''),
                "cause_by": getattr(msg, 'cause_by', ''),
                "timestamp": datetime.now().isoformat(),
            })
        return result

    # ========== 属性 ==========

    def __len__(self) -> int:
        return len(self._buffer)

    @property
    def is_empty(self) -> bool:
        return len(self._buffer) == 0
