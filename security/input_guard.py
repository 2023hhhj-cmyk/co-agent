"""
InputGuard — 输入校验层

在所有外部输入入口进行统一安全校验：
1. 长度限制 (≤ 5000 chars)
2. Prompt 注入检测（含 Unicode 归一化 + 零宽字符处理）
3. SQL 注入模式检测
4. HTML/JS 注入净化
5. 空内容拒绝

P0 #7 修复：
  - 增加 Unicode NFKC 归一化（防同形字绕过）
  - 增加零宽字符检测（ZWSJ/ZWSP 等）
  - 增加编码混淆检测（base64、十六进制 escape）
  - 增加 markdown 隐藏指令检测

使用方式：
  guard = InputGuard()
  result = guard.validate(content, source="feishu")
  if not result.allowed:
      logger.warning(f"输入被拒绝: {result.reason}")
"""

import logging
import re
import unicodedata
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
    # P0 #7 新增：中文变体
    r"忽略.{0,5}(之前|上面|以上).{0,5}(指令|指示|提示)",
    r"忘记.{0,5}(所有|全部).{0,5}(指令|指示)",
    r"你现在是",
    r"你扮演",
    r"假装你是",
    # P0 #7 新增：工具调用注入
    r"<function_calls>",
    r"<tool_call",
    r"<antml:function_calls>",
    r"```tool_call",
]

# 零宽字符（用于混淆绕过）
# 用 \u 转义而非字面量字符，避免控制字符在 regex 编译时触发 unterminated set
ZERO_WIDTH_PATTERN = re.compile(
    r"[​-‏"   # ZWSP, ZWNJ, ZWJ, LRM, RLM
    r"‪-‮"   # LRE, RLE, PDF, LRO, RLO
    r"﻿"          # BOM / ZWNBSP
    r"⁠-⁤"   # WJ, FUNCTION APPLICATION, INVISIBLE TIMES/SEPARATOR/PLUS
    r"]"
)

# 编码混淆模式
ENCODING_OBFUSCATION_PATTERNS = [
    r"data:text/plain;base64,[A-Za-z0-9+/=]{20,}",
    # hex escape 序列：匹配 \xNN 形式（regex 中 \\x 表示字面量 \x）
    r"\\x[0-9a-fA-F]{2}(?:\\x[0-9a-fA-F]{2}){5,}",
    r"&#x?[0-9a-fA-F]+;{3,}",  # HTML entity encoding
    r"\\u[0-9a-fA-F]{4}(?:\\u[0-9a-fA-F]{4}){5,}",
]

# Markdown 隐藏指令
MARKDOWN_HIDDEN_PATTERNS = [
    r"<!--[^\n]*(ignore|system|forget|disregard)[^\n]*-->",
    # 用非 raw 字符串写 4 个反斜杠 = 2 个实际反斜杠（regex 中 \\ 表示字面量 \）
    # 匹配 [\!\[ 这类被转义字符包裹的链接混淆（攻击者常用）
    "\\[\\\\!\\\\\\[",
    r"<details[^>]*>[^<]*<summary[^>]*>[^<]*(ignore|system|forget)[^<]*</summary>",
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
        self._encoding_patterns = [re.compile(p, re.IGNORECASE) for p in ENCODING_OBFUSCATION_PATTERNS]
        self._markdown_hidden = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in MARKDOWN_HIDDEN_PATTERNS]

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

        # 2. P0 #7：检测零宽字符（混淆绕过手段）
        if ZERO_WIDTH_PATTERN.search(content):
            rejected_counter.labels(source=source, reason="zero_width_chars").inc()
            logger.warning(f"[InputGuard] 零宽字符检测: source={source}")
            return ValidationResult(
                allowed=False,
                content="",
                reason="输入包含零宽字符（疑似混淆）",
                flags=["zero_width_chars"],
            )

        # 3. P0 #7：Unicode NFKC 归一化（仅用于模式匹配，不修改输出）
        # 关键：NFKC 会把全角字符变半角（如 ，→ ,），这会破坏原文。
        # 所以用 normalized 副本做模式匹配，cleaned 保留原字符。
        normalized = unicodedata.normalize("NFKC", content)
        if normalized != content:
            flags.append("unicode_normalized")

        cleaned = content.strip()  # 保留原始字符

        # 4. 长度限制（基于原始 cleaned）
        if len(cleaned) > self.max_length:
            cleaned = cleaned[:self.max_length]
            flags.append("truncated")
            logger.info(f"[InputGuard] 输入截断: {len(content)} → {self.max_length} chars, source={source}")

        # 5. Prompt 注入检测（在归一化后的内容上，能识别全角同形字）
        for pattern in self._prompt_patterns:
            if pattern.search(normalized):
                logger.warning(f"[InputGuard] Prompt 注入检测: source={source}, pattern={pattern.pattern[:50]}...")
                rejected_counter.labels(source=source, reason="prompt_injection").inc()
                return ValidationResult(
                    allowed=False,
                    content=cleaned,
                    reason="检测到 prompt 注入模式",
                    flags=["prompt_injection"],
                )

        # 6. 编码混淆检测（在归一化内容上）
        for pattern in self._encoding_patterns:
            if pattern.search(normalized):
                logger.warning(f"[InputGuard] 编码混淆检测: source={source}, pattern={pattern.pattern[:50]}...")
                rejected_counter.labels(source=source, reason="encoding_obfuscation").inc()
                return ValidationResult(
                    allowed=False,
                    content=cleaned,
                    reason="检测到编码混淆（疑似绕过）",
                    flags=["encoding_obfuscation"],
                )

        # 7. Markdown 隐藏指令检测（在归一化内容上）
        for pattern in self._markdown_hidden:
            if pattern.search(normalized):
                logger.warning(f"[InputGuard] Markdown 隐藏指令检测: source={source}")
                rejected_counter.labels(source=source, reason="markdown_hidden").inc()
                return ValidationResult(
                    allowed=False,
                    content=cleaned,
                    reason="检测到 Markdown 隐藏指令",
                    flags=["markdown_hidden"],
                )

        # 8. SQL 注入检测
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

        # 9. HTML/JS 净化（不拒绝，但移除危险标签）
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
