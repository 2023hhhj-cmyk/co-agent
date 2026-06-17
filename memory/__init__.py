"""
记忆系统 — 三层记忆架构 + 智能强化

┌──────────────────────────────────────┐
│         MemoryManager（统一入口）      │
├──────────────────────────────────────┤
│  Layer 1: WorkingMemory  短期工作记忆  │
│  Layer 2: SessionMemory  跨会话历史    │
│  Layer 3: KnowledgeMemory 语义知识库  │
├──────────────────────────────────────┤
│  ImportanceScorer      重要性评分器    │
│  MemoryConsolidator    自动提炼器      │
│  MemoryGraph           关联图谱        │
└──────────────────────────────────────┘
"""

from memory.working import WorkingMemory
from memory.session import SessionMemory
from memory.knowledge import KnowledgeMemory
from memory.manager import MemoryManager, MemoryItem
from memory.importance import ImportanceScorer
from memory.consolidator import MemoryConsolidator
from memory.graph import MemoryGraph

__all__ = [
    "WorkingMemory",
    "SessionMemory",
    "KnowledgeMemory",
    "MemoryManager",
    "MemoryItem",
    "ImportanceScorer",
    "MemoryConsolidator",
    "MemoryGraph",
]
