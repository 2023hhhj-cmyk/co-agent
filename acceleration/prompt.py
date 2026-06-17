"""
Prompt 优化器

1. 动态压缩：消息列表 > 20 条时，压缩早期消息为摘要
2. 模板预编译：减少 system prompt 拼接开销

压缩策略：
  原始: [sys] + m1 + m2 + ... + m25 + [user]
  压缩: [sys] + "[前15条对话摘要]" + m16 + ... + m25 + [user]
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 压缩阈值
MAX_MESSAGES_BEFORE_COMPRESS = 20
KEEP_RECENT = 10           # 保留最近 N 条完整消息


class PromptOptimizer:
    """Prompt 优化器"""

    def __init__(self, llm_client: Any = None):
        """
        Args:
            llm_client: 用于压缩时调用 LLM 生成摘要（可选）
        """
        self.llm = llm_client

    def compress_messages(
        self,
        messages: List[Dict[str, str]],
        max_total: int = MAX_MESSAGES_BEFORE_COMPRESS,
        keep_recent: int = KEEP_RECENT,
    ) -> List[Dict[str, str]]:
        """
        压缩消息列表

        Args:
            messages: [{"role": "user/assistant/system", "content": "..."}, ...]
            max_total: 超过此值才压缩
            keep_recent: 保留最近 N 条完整消息

        Returns:
            压缩后的消息列表
        """
        if len(messages) <= max_total:
            return messages

        # 分离 system prompt 和对话
        system_msgs = [m for m in messages if m.get("role") == "system"]
        dialog_msgs = [m for m in messages if m.get("role") != "system"]

        if len(dialog_msgs) <= keep_recent:
            return messages

        # 早期消息压缩为摘要
        early = dialog_msgs[:-keep_recent]
        recent = dialog_msgs[-keep_recent:]

        summary = self._build_summary(early)

        result = system_msgs + [
            {"role": "system", "content": f"[早期对话摘要 ({len(early)} 条消息)]\n{summary}"}
        ] + recent

        logger.debug(f"Prompt 压缩: {len(messages)} → {len(result)} messages")
        return result

    def _build_summary(self, messages: List[Dict[str, str]]) -> str:
        """从早期消息构建摘要"""
        lines = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            # 截取前100字符
            snippet = content[:100] + "..." if len(content) > 100 else content
            lines.append(f"[{role}]: {snippet}")
        return "\n".join(lines)

    async def compress_with_llm(
        self,
        messages: List[Dict[str, str]],
        max_total: int = MAX_MESSAGES_BEFORE_COMPRESS,
    ) -> List[Dict[str, str]]:
        """
        使用 LLM 生成压缩摘要（更精确但消耗一次 API 调用）
        """
        if len(messages) <= max_total or not self.llm:
            return self.compress_messages(messages, max_total)

        system_msgs = [m for m in messages if m.get("role") == "system"]
        dialog_msgs = [m for m in messages if m.get("role") != "system"]

        if len(dialog_msgs) <= KEEP_RECENT:
            return messages

        early = dialog_msgs[:-KEEP_RECENT]
        recent = dialog_msgs[-KEEP_RECENT:]

        # 调 LLM 生成摘要
        try:
            early_text = "\n".join(
                f"[{m['role']}]: {m.get('content', '')[:200]}"
                for m in early
            )
            llm_messages = [
                {"role": "system", "content": "用 2-3 句话总结以下对话的关键信息，只输出摘要。"},
                {"role": "user", "content": early_text},
            ]
            summary = await self.llm.chat(llm_messages, temperature=0.1, max_tokens=200)
        except Exception as e:
            logger.warning(f"LLM 摘要生成失败，使用简单压缩: {e}")
            summary = self._build_summary(early)

        result = system_msgs + [
            {"role": "system", "content": f"[{len(early)} 条早期消息的摘要]\n{summary}"}
        ] + recent

        logger.debug(f"Prompt LLM 压缩: {len(messages)} → {len(result)} messages")
        return result
