"""
LLM 客户端封装

统一接口支持 DeepSeek，提供：
- chat: 基础对话
- chat_with_json: 结构化 JSON 输出
- chat_with_tools: 工具调用
"""

import json
import logging
import math
import random
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from asyncio import sleep
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

import httpx

from config.settings import settings
from tools.llm.rate_limit import get_llm_rate_limiter

logger = logging.getLogger(__name__)

# OpenCode Go 网关要求稳定的会话标识，用于路由与提示缓存
_OPENCODE_SESSION_ID = f"ses_coagent_{uuid.uuid4().hex[:16]}"

# 每次调用的 usage 收集器：按协程隔离，避免并发请求互相覆盖
_usage_sink: ContextVar[Optional[Dict[str, Any]]] = ContextVar("llm_usage_sink", default=None)


@contextmanager
def collect_usage():
    """with collect_usage() as usage: 之后可读到本次所有 LLM 调用的累计用量。"""
    sink: Dict[str, Any] = {}
    token = _usage_sink.set(sink)
    try:
        yield sink
    finally:
        _usage_sink.reset(token)



class LLMClient(ABC):
    """LLM 客户端基类（含缓存支持）"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Any = None  # ResponseCache 实例
        # ponytail: last API usage captured for analytics (agent.think tokens).
        self._last_usage: Dict[str, int] = {}

    def get_last_usage(self) -> Dict[str, int]:
        """返回最近一次 chat 的 token usage（缓存命中时为空）。"""
        return dict(self._last_usage)

    def set_cache(self, cache: Any) -> None:
        """注入响应缓存"""
        self._cache = cache

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _check_cache(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        use_cache: bool = True,
    ) -> Optional[str]:
        """检查缓存"""
        if not self._cache or not use_cache or temperature > 0.5:
            return None
        prompt = self._build_prompt_string(messages)
        return await self._cache.get(prompt, self.model, temperature)

    async def _store_cache(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        response: str,
        success: bool,
    ):
        """存储到缓存"""
        if self._cache and success:
            prompt = self._build_prompt_string(messages)
            await self._cache.set(prompt, self.model, temperature, response, success)

    def _build_prompt_string(self, messages: List[Dict[str, str]]) -> str:
        """构建 prompt 字符串用于缓存 key"""
        # Full messages prevent different long requirements or earlier context sharing a cache key.
        return json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    async def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        use_cache: bool = True,
        **kwargs,
    ) -> str:
        """基础对话（含缓存检查）"""
        # 检查缓存
        cached = await self._check_cache(messages, temperature, use_cache)
        if cached:
            # ponytail: cache hit = 0 new tokens, clear so analytics stays accurate
            self._last_usage = {}
            return cached

        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        # Conservative text estimate, including tools/kwargs and message framing.
        # This is a UTF-8 byte bound estimate, not the provider's exact tokenizer.
        payload = {"model": self.model, "messages": messages, **kwargs}
        estimated_input = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        estimated_input += 32 * len(messages)
        limiter = get_llm_rate_limiter()
        for attempt in range(settings.LLM_MAX_RETRIES + 1):
            # Every attempt, including retries, must acquire RPM/TPM budget.
            reservation = await limiter.acquire(estimated_input + max_tokens)
            actual_tokens = None
            retry_error = None
            try:
                call_usage: Dict[str, Any] = {}
                result = await self._chat_impl(
                    messages, temperature, max_tokens, usage_out=call_usage, **kwargs,
                )
                # 本次调用自己的用量：共享 client 时不会被并发请求覆盖
                usage = dict(call_usage) or dict(self._last_usage)
                _sink = _usage_sink.get()
                if _sink is not None:
                    for _key, _value in usage.items():
                        if isinstance(_value, (int, float)):
                            _sink[_key] = _sink.get(_key, 0) + _value
                if "total_tokens" in usage:
                    actual_tokens = usage["total_tokens"]
                elif "prompt_tokens" in usage and "completion_tokens" in usage:
                    actual_tokens = usage["prompt_tokens"] + usage["completion_tokens"]
            except Exception as exc:
                self._last_usage = {}
                if attempt >= settings.LLM_MAX_RETRIES or not self._is_retryable(exc):
                    raise
                retry_error = exc
            except BaseException:
                self._last_usage = {}
                raise
            finally:
                # Unknown usage retains its reservation, even on cancellation.
                await limiter.finish(reservation, actual_tokens)

            if retry_error is None:
                # use_cache=False 表示既不读缓存也不写缓存
                if use_cache:
                    await self._store_cache(messages, temperature, result, success=True)
                return result

            delay = self._retry_delay(retry_error, attempt)
            # Never log request headers, body or exception text containing credentials.
            logger.warning(
                "LLM transient failure (%s), retry %d/%d in %.2fs",
                type(retry_error).__name__, attempt + 1, settings.LLM_MAX_RETRIES, delay,
            )
            await sleep(delay)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {408, 429, 500, 502, 503, 504, 529}
        return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError,
                                httpx.RemoteProtocolError))

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        base = min(settings.LLM_RETRY_MAX_DELAY,
                   settings.LLM_RETRY_BASE_DELAY * 2 ** attempt)
        delay = min(settings.LLM_RETRY_MAX_DELAY, base + random.uniform(0, base * 0.25))
        if isinstance(exc, httpx.HTTPStatusError):
            header = exc.response.headers.get("Retry-After")
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    try:
                        deadline = parsedate_to_datetime(header)
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=timezone.utc)
                        retry_after = (deadline - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        retry_after = 0
                if math.isfinite(retry_after):
                    # Provider's minimum wait takes precedence over our backoff ceiling.
                    delay = max(delay, retry_after)
        return delay

    @abstractmethod
    async def _chat_impl(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> str:
        """实际 API 调用（子类实现）"""
        pass

    async def chat_with_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs,
    ) -> Dict:
        """结构化对话，返回 JSON

        P2 #20 修复：增强解析鲁棒性
          - 支持 markdown 代码块（多种语言标签）
          - 支持 LLM 在 JSON 前后加的散文（如 "Here's the answer: {...}"）
          - 多次尝试不同剥离策略
          - 失败时不静默：返回包含 _parse_error 字段的字典便于调试
        """
        # 在 user message 末尾添加 JSON 输出要求
        modified_messages = messages.copy()
        if modified_messages and modified_messages[-1]["role"] == "user":
            modified_messages[-1]["content"] += "\n\n请以 JSON 格式输出，不要包含其他文本。"

        response = await self.chat(
            modified_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

        # P2 #20：多种策略依次尝试
        text = response.strip()
        candidates = self._extract_json_candidates(text)

        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

        # 全部失败：不静默，记录错误详情
        logger.warning(
            f"[P2 #20] Failed to parse JSON from LLM response. "
            f"Tried {len(candidates)} candidates. Response preview: {response[:300]}"
        )
        return {
            "raw_response": response,
            "_parse_error": "Failed to extract valid JSON",
        }

    @staticmethod
    def _extract_json_candidates(text: str) -> List[str]:
        """从 LLM 响应中提取可能的 JSON 候选

        策略（按优先级）：
        1. 完整文本（如果本身就是合法 JSON）
        2. 去掉 markdown 代码块包裹
        3. 提取第一个 {...} 块
        4. 提取第一个 [...] 块
        """
        candidates: List[str] = []

        # 1. 原文
        candidates.append(text)

        # 2. 去掉 markdown 代码块
        cleaned = text
        if cleaned.startswith("```"):
            # 去掉开头的 ```json / ``` / ```JSON 等
            first_newline = cleaned.find("\n")
            if first_newline > 0:
                cleaned = cleaned[first_newline + 1:]
            else:
                cleaned = cleaned[3:]
            # 去掉结尾的 ```
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
            if cleaned != text:
                candidates.append(cleaned)

        # 3. 提取第一个 {...} 块
        obj_start = text.find("{")
        obj_end = text.rfind("}")
        if obj_start != -1 and obj_end > obj_start:
            obj_str = text[obj_start:obj_end + 1]
            if obj_str not in candidates:
                candidates.append(obj_str)

        # 4. 提取第一个 [...] 块
        arr_start = text.find("[")
        arr_end = text.rfind("]")
        if arr_start != -1 and arr_end > arr_start:
            arr_str = text[arr_start:arr_end + 1]
            if arr_str not in candidates:
                candidates.append(arr_str)

        return candidates


class DeepSeekClient(LLMClient):
    """DeepSeek API 客户端（OpenAI 兼容格式）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__(
            api_key=api_key or settings.DEEPSEEK_API_KEY,
            base_url=base_url or settings.DEEPSEEK_BASE_URL,
            model=model or settings.DEEPSEEK_MODEL,
        )

    async def _chat_impl(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> str:
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # OpenCode Go 网关（opencode.ai/inference）要求 x-opencode-session 才能路由
        if "opencode.ai" in (self.base_url or ""):
            headers["x-opencode-session"] = _OPENCODE_SESSION_ID
            headers["User-Agent"] = "co-agent/1.0 (+opencode-go)"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            self._last_usage = data.get("usage", {}) or {}
            if usage_out is not None:
                usage_out.update(self._last_usage)
            message = data["choices"][0]["message"]
            # 推理模型（如 deepseek-v4-pro）可能把内容放在 reasoning_content 中
            content = message.get("content", "")
            if not content and message.get("reasoning_content"):
                content = message["reasoning_content"]
            return content
        except Exception as e:
            logger.error(f"DeepSeek API call failed: {type(e).__name__}", exc_info=True)
            raise


class ClaudeClient(LLMClient):
    """Anthropic Claude API 客户端"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
    ):
        super().__init__(
            api_key=api_key or settings.ANTHROPIC_API_KEY,
            base_url="https://api.anthropic.com",
            model=model,
        )

    async def _chat_impl(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        usage_out: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> str:
        client = await self._get_client()
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        # 分离 system message
        system_msg = ""
        chat_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                chat_messages.append(msg)

        payload = {
            "model": self.model,
            "messages": chat_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_msg:
            payload["system"] = system_msg

        try:
            response = await client.post(
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            # Anthropic uses input_tokens/output_tokens; map to OpenAI-style for unified analytics
            _u = data.get("usage", {}) or {}
            self._last_usage = {
                "prompt_tokens": _u.get("input_tokens", 0),
                "completion_tokens": _u.get("output_tokens", 0),
            }
            if usage_out is not None:
                usage_out.update(self._last_usage)
            return data["content"][0]["text"]
        except Exception as e:
            logger.error(f"Claude API call failed: {type(e).__name__}", exc_info=True)
            raise
