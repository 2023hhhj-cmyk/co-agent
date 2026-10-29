"""
MemoryConsolidator — LLM 驱动的记忆自动提炼器

功能：
- 将原始消息流提炼为结构化摘要（summary + key_decisions + technical_notes + outcome + tags）
- 异步非阻塞：先快速归档原始数据，后台提炼完成后更新记忆
- LLM 不可用时静默降级

提炼结果用途：
- summary → 更新 SessionMemory（替代原始消息流）
- key_decisions → 存入 KnowledgeMemory（长期经验）
- technical_notes → 存入 KnowledgeMemory（技术积累）
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from analytics import emit_event  # 埋点（agent 简历数据采集）
except Exception:  # noqa: BLE001
    def emit_event(*_a, **_kw):  # type: ignore[no-redef]
        return None

logger = logging.getLogger(__name__)

# 提炼 Prompt 模板
CONSOLIDATION_SYSTEM_PROMPT = """你是一个记忆提炼器。从以下 Agent 工作日志中提炼关键信息。

## 提炼规则
1. summary：用 2-3 句话总结这个任务做了什么、为什么做、结果如何
2. key_decisions：列出任务中做出的重要决策（技术选型、架构选择、需求变更等），最多 3 条
3. technical_notes：列出值得记录的技术要点（踩坑、最佳实践、工具使用），最多 3 条
4. outcome：判断最终结果（succeeded/partial/failed）
5. tags：3-5 个标签，用于后续检索分类

## 输出格式
严格输出 JSON，不要包含其他文本：
{
  "summary": "...",
  "key_decisions": ["决策1", "决策2"],
  "technical_notes": ["技术要点1"],
  "outcome": "succeeded",
  "tags": ["标签1", "标签2", "标签3"]
}

## 注意
- 只提炼有价值的信息，忽略琐碎对话
- 如果日志中没有关键信息，key_decisions 和 technical_notes 可以为空数组
- 标签尽量用英文或中文关键词
"""


def _repair_truncated_json(text: str) -> Optional[str]:
    """把被截断的 JSON 补成可解析的形式（补引号、闭合未闭合的括号）。"""
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:].rstrip()
    if text.endswith(","):
        text = text[:-1]
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                stack.append(ch)
            elif ch in "]}":
                if stack:
                    stack.pop()
    if in_string:
        text += '"'
    text += "".join("]" if c == "[" else "}" for c in reversed(stack))
    return text


class MemoryConsolidator:
    """LLM 驱动的记忆提炼器"""

    def __init__(self, llm_client: Any = None):
        """
        Args:
            llm_client: LLMClient 实例，不提供则跳过提炼
        """
        self.llm = llm_client
        self._running_tasks: set[asyncio.Task] = set()

    # ========== 核心提炼 ==========

    async def consolidate(
        self,
        messages: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        提炼原始消息流为结构化摘要

        Args:
            messages: [{"content": "...", "sent_from": "...", "cause_by": "..."}, ...]
            metadata: 额外上下文（agent_role, task_name 等）

        Returns:
            提炼结果 dict，LLM 不可用时返回 None
        """
        if not self.llm:
            logger.debug("LLM 客户端不可用，跳过提炼")
            return None

        if not messages:
            return None

        _start = time.time()
        _items_in = len(messages)

        # 构建原始日志文本
        log_lines = []
        for i, msg in enumerate(messages):
            sender = msg.get("sent_from", "unknown")
            content = msg.get("content", str(msg))
            log_lines.append(f"[{i}] [{sender}] {content}")

        raw_log = "\n".join(log_lines)

        # 构建 user prompt
        user_parts = [f"## 原始日志 ({len(messages)} 条消息)\n{raw_log}"]

        if metadata:
            user_parts.insert(0, f"## 上下文\n- Agent: {metadata.get('agent_role', 'unknown')}\n- 任务: {metadata.get('task_name', 'unknown')}")

        user_prompt = "\n\n".join(user_parts)

        # 调用 LLM
        try:
            llm_messages = [
                {"role": "system", "content": CONSOLIDATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            response = await self.llm.chat(
                llm_messages,
                temperature=0.2,
                max_tokens=1500,
                # 提炼是一次性转换，缓存既浪费内存，也会把截断结果反复喂回来
                use_cache=False,
            )

            result = self._parse_response(response)
            # 埋点：memory.distill（成功）
            _role = (metadata or {}).get("agent_role", "unknown")
            _items_out = 0
            if result:
                _items_out = sum(
                    len(v) for v in result.values() if isinstance(v, list)
                ) or 1
            emit_event("memory.distill", agent_role=_role, items_in=_items_in, items_out=_items_out,
                       duration_ms=int((time.time() - _start) * 1000))
            return result

        except Exception as e:
            logger.error(f"LLM 提炼失败: {e}")
            # 埋点：memory.distill（失败）
            _role = (metadata or {}).get("agent_role", "unknown")
            emit_event("memory.distill", agent_role=_role, items_in=_items_in, items_out=0,
                       duration_ms=int((time.time() - _start) * 1000))
            return None

    # ========== 异步非阻塞提炼 ==========

    def consolidate_async(
        self,
        messages: List[Dict[str, Any]],
        callback: callable,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        异步提炼（fire-and-forget），完成后回调

        Args:
            messages: 原始消息列表
            callback: 提炼完成后的回调 async def callback(result: dict | None)
            metadata: 额外上下文
        """
        async def _run():
            try:
                result = await self.consolidate(messages, metadata)
                await callback(result)
            except Exception as e:
                logger.error(f"异步提炼异常: {e}")
                try:
                    await callback(None)
                except Exception:
                    pass

        task = asyncio.create_task(_run())
        self._running_tasks.add(task)
        task.add_done_callback(self._running_tasks.discard)

    # ========== JSON 解析 ==========

    def _parse_response(self, response: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 JSON"""
        text = response.strip()

        # 去掉 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            # 去掉首行的 ```json 或 ```
            if lines[0].startswith("```"):
                lines = lines[1:]
            # 去掉末行的 ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                repaired = _repair_truncated_json(text)
                if not repaired:
                    raise
                data = json.loads(repaired)
                logger.warning("提炼结果 JSON 被截断，已补全结构后解析")
            # 验证必要字段
            if "summary" not in data:
                logger.warning("LLM 返回的 JSON 缺少 summary 字段")
                return None

            # 确保字段类型正确
            return {
                "summary": str(data.get("summary", "")),
                "key_decisions": data.get("key_decisions", []) or [],
                "technical_notes": data.get("technical_notes", []) or [],
                "outcome": str(data.get("outcome", "unknown")),
                "tags": data.get("tags", []) or [],
                "consolidated_at": datetime.now().isoformat(),
            }
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 返回的 JSON 解析失败: {e}, 原始: {text[:200]}")
            return None

    # ========== 属性 ==========

    @property
    def is_ready(self) -> bool:
        return self.llm is not None

    @property
    def pending_count(self) -> int:
        return len(self._running_tasks)
