"""
InputGuard — 输入校验层

在所有外部输入入口进行统一安全校验：
1. 长度限制 (≤ 5000 chars)
2. Prompt 注入检测
3. SQL 注入模式检测
4. HTML/JS 注入净化
5. 空内容拒绝

使用方式：
  guard = InputGuard()
  result = guard.validate(content, source="feishu")
  if not result.allowed:
      logger.warning(f"输入被拒绝: {result.reason}")
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from observability.metrics import input_rejected as rejected_counter

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 5000
MIN_CONTENT_LENGTH = 1

# Prompt 注入特征模式
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+(instructions?|prompts?|context)",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\[system\]\(#system\)",
    r"\[INST\]",
    r"\[/INST\]",
    r"<<SYS>>",
    r"<</SYS>>",
    r"system:\s*you\s+are\s+now",
    r"you\s+are\s+a\s+new\s+(ai|assistant|agent)",
    r"forget\s+(all\s+)?(your\s+)?instructions",
    r"disregard\s+(all\s+)?(previous\s+)?instructions",
    r"new\s+system\s+prompt",
    r"override\s+(the\s+)?system",
]

# SQL 注入特征模式
SQL_INJECTION_PATTERNS = [
    r"'\s*;\s*(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE)\s+",
    r"\bUNION\s+SELECT\b",
    r"'\s+OR\s+'1'='1",
    r"'\s+OR\s+1=1",
    r"--\s*$",
    r"/\*.*\*/",
    r"\bEXEC\s*\(.*\)",
    r"\bEXECUTE\s+",
]

# HTML/JS 注入特征
HTML_JS_PATTERNS = [
    r"<script[^>]*>.*?</script>",
    r"javascript\s*:",
    r"on\w+\s*=\s*\"[^\"]*\"",
    r"on\w+\s*=\s*'[^']*'",
    r"<iframe[^>]*>",
    r"<embed[^>]*>",
    r"<object[^>]*>",
]


@dataclass
class ValidationResult:
    """校验结果"""
    allowed: bool
    content: str                    # 校验后内容（可能被截断/净化）
    reason: str = ""                # 拒绝原因
    flags: List[str] = field(default_factory=list)  # 触发的规则


class InputGuard:
    """输入安全校验器"""

    def __init__(
        self,
        max_length: int = MAX_CONTENT_LENGTH,
        min_length: int = MIN_CONTENT_LENGTH,
    ):
        self.max_length = max_length
        self.min_length = min_length

        # 编译正则
        self._prompt_patterns = [re.compile(p, re.IGNORECASE) for p in PROMPT_INJECTION_PATTERNS]
        self._sql_patterns = [re.compile(p, re.IGNORECASE) for p in SQL_INJECTION_PATTERNS]
        self._html_patterns = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in HTML_JS_PATTERNS]

    def validate(self, content: str, source: str = "unknown") -> ValidationResult:
        """
        校验输入内容

        Args:
            content: 原始输入
            source: 输入来源 (feishu/wecom/api/unknown)

        Returns:
            ValidationResult
        """
        flags = []

        # 1. 空内容检查
        if not content or len(content.strip()) < self.min_length:
            rejected_counter.labels(source=source, reason="empty").inc()
            return ValidationResult(
                allowed=False,
                content="",
                reason="输入为空",
                flags=["empty_content"],
            )

        cleaned = content.strip()

        # 2. 长度限制
        if len(cleaned) > self.max_length:
            cleaned = cleaned[:self.max_length]
            flags.append("truncated")
            logger.info(f"[InputGuard] 输入截断: {len(content)} → {self.max_length} chars, source={source}")

        # 3. Prompt 注入检测
        for pattern in self._prompt_patterns:
            if pattern.search(cleaned):
                logger.warning(f"[InputGuard] Prompt 注入检测: source={source}, pattern={pattern.pattern[:50]}...")
                rejected_counter.labels(source=source, reason="prompt_injection").inc()
                return ValidationResult(
                    allowed=False,
                    content=cleaned,
                    reason=f"检测到 prompt 注入模式",
                    flags=["prompt_injection"],
                )

        # 4. SQL 注入检测
        for pattern in self._sql_patterns:
            if pattern.search(cleaned):
                logger.warning(f"[InputGuard] SQL 注入检测: source={source}")
                rejected_counter.labels(source=source, reason="sql_injection").inc()
                return ValidationResult(
                    allowed=False,
                    content=cleaned,
                    reason="检测到 SQL 注入模式",
                    flags=["sql_injection"],
                )

        # 5. HTML/JS 净化（不拒绝，但移除危险标签）
        for pattern in self._html_patterns:
            if pattern.search(cleaned):
                cleaned = pattern.sub("[REMOVED]", cleaned)
                flags.append("html_stripped")
                logger.info(f"[InputGuard] HTML/JS 已净化: source={source}")

        return ValidationResult(
            allowed=True,
            content=cleaned,
            flags=flags,
        )

    def safe_validate(self, content: str, source: str = "unknown") -> str:
        """
        安全校验并返回净化后内容（快捷方法）

        拒绝时返回空字符串，调用方需检查。
        """
        result = self.validate(content, source)
        return result.content if result.allowed else ""
