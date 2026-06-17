"""
LLM 客户端封装

统一接口支持 DeepSeek，提供：
- chat: 基础对话
- chat_with_json: 结构化 JSON 输出
- chat_with_tools: 工具调用
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """LLM 客户端基类（含缓存支持）"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Any = None  # ResponseCache 实例

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
        return "\n".join(f"{m['role']}:{m.get('content', '')[:200]}" for m in messages[-10:])

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
            return cached

        # 调实际 API
        try:
            result = await self._chat_impl(messages, temperature, max_tokens, **kwargs)
            await self._store_cache(messages, temperature, result, success=True)
            return result
        except Exception:
            await self._store_cache(messages, temperature, "", success=False)
            raise

    @abstractmethod
    async def _chat_impl(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
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
        """结构化对话，返回 JSON"""
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

        # 尝试解析 JSON
        try:
            # 处理可能的 markdown 代码块
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON from LLM response: {response[:200]}")
            return {"raw_response": response}


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
        **kwargs,
    ) -> str:
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
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
            return data["content"][0]["text"]
        except Exception as e:
            logger.error(f"Claude API call failed: {type(e).__name__}", exc_info=True)
            raise
