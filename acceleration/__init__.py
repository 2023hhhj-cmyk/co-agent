"""
加速层 — LLM 缓存 + Prompt 优化 + 嵌入缓存

┌──────────────────────────────────────────────┐
│  ResponseCache      LLM 响应缓存 (Redis)      │
│  PromptOptimizer    Prompt 动态压缩            │
│  EmbeddingCache     嵌入向量缓存 (Redis)       │
└──────────────────────────────────────────────┘
"""

from acceleration.cache import ResponseCache, EmbeddingCache
from acceleration.prompt import PromptOptimizer

__all__ = [
    "ResponseCache",
    "EmbeddingCache",
    "PromptOptimizer",
]
