"""
ImportanceScorer — 多维记忆重要性评分器

评分维度（权重可配置）：
1. 来源角色 (30%): CEO 决策 > Developer 经验 > 日常对话
2. 记忆类型 (25%): experience > best_practice > decision > task > chat > raw_log
3. 内容质量 (20%): 长度适中的内容得分更高，极短/过长得低分
4. 时间衰减 (15%): 艾宾浩斯遗忘曲线近似，旧记忆逐渐降权
5. 访问热度 (10%): 被检索越频繁越重要

输出：0.0-1.0 之间的重要性分数
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ========== 权重配置 ==========

# 来源角色权重
ROLE_WEIGHTS = {
    "ceo": 1.0,
    "developer": 0.8,
    "devops": 0.7,
    "frontend_expert": 0.75,
    "customer_service": 0.5,
}

# 记忆类型权重
TYPE_WEIGHTS = {
    "experience": 1.0,
    "best_practice": 0.9,
    "decision": 0.85,
    "knowledge": 0.8,
    "project_knowledge": 0.75,
    "faq": 0.7,
    "task": 0.6,
    "chat": 0.3,
    "raw_log": 0.1,
    "working": 0.2,
}

# 各维度占比
DIMENSION_WEIGHTS = {
    "role": 0.30,
    "type": 0.25,
    "quality": 0.20,
    "decay": 0.15,
    "access": 0.10,
}


class ImportanceScorer:
    """多维记忆重要性评分器"""

    def __init__(
        self,
        role_weights: Optional[dict] = None,
        type_weights: Optional[dict] = None,
        dimension_weights: Optional[dict] = None,
    ):
        """
        Args:
            role_weights: 自定义角色权重
            type_weights: 自定义记忆类型权重
            dimension_weights: 自定义维度占比
        """
        self.role_weights = role_weights or ROLE_WEIGHTS
        self.type_weights = type_weights or TYPE_WEIGHTS
        self.dim_weights = dimension_weights or DIMENSION_WEIGHTS

    # ========== 各维度计算 ==========

    def _score_role(self, agent_role: str) -> float:
        """来源角色评分"""
        role = (agent_role or "").lower()
        return self.role_weights.get(role, 0.3)

    def _score_type(self, memory_type: str) -> float:
        """记忆类型评分"""
        mtype = (memory_type or "task").lower()
        return self.type_weights.get(mtype, 0.4)

    def _score_quality(self, content: str) -> float:
        """
        内容质量评分（基于长度启发式）

        极短消息（"知道了"）通常是低价值信号
        极长消息可能包含噪声
        中等长度消息（50-500字）最有可能是经过思考的内容
        """
        if not content:
            return 0.0

        length = len(content)

        if length < 10:
            return 0.1   # "OK", "好的", "知道了"
        elif length < 50:
            return 0.4   # 简短回复
        elif length < 200:
            return 0.7   # 适中内容
        elif length < 500:
            return 0.9   # 有料内容
        elif length < 2000:
            return 0.8   # 详细内容
        else:
            return 0.6   # 过长，可能是日志堆砌

    def _score_decay(self, created_at: Optional[str]) -> float:
        """
        时间衰减评分（艾宾浩斯遗忘曲线近似）

        score = 1.0 / (1.0 + days_since_creation / 30)

        第 0 天: 1.00
        第 7 天: 0.81
        第 30 天: 0.50
        第 90 天: 0.25
        第 365 天: 0.08
        """
        if not created_at:
            return 0.5  # 无时间信息给中值

        try:
            if created_at.endswith("Z"):
                created_at = created_at[:-1] + "+00:00"
            created_dt = datetime.fromisoformat(created_at)
            now = datetime.now(timezone.utc)
            # 确保两个 datetime 都有时区信息
            if created_dt.tzinfo is None:
                from datetime import timezone as tz
                created_dt = created_dt.replace(tzinfo=tz.utc)
            days = (now - created_dt).total_seconds() / 86400.0
            days = max(0, days)

            return 1.0 / (1.0 + days / 30.0)
        except (ValueError, TypeError) as e:
            logger.debug(f"时间解析失败: {created_at!r}, error={e}")
            return 0.5

    def _score_access(self, access_count: int) -> float:
        """访问热度评分"""
        return min(1.0, max(0.0, access_count / 10.0))

    # ========== 综合评分 ==========

    def score(
        self,
        content: str,
        agent_role: str = "",
        memory_type: str = "task",
        created_at: Optional[str] = None,
        access_count: int = 0,
        explicit_importance: Optional[float] = None,
    ) -> float:
        """
        计算综合重要性分数

        Args:
            content: 记忆内容
            agent_role: Agent 角色
            memory_type: 记忆类型 (experience/task/chat/...)
            created_at: 创建时间 (ISO 格式)
            access_count: 历史访问次数
            explicit_importance: 显式标记的重要性 (0.0-1.0)，优先级最高

        Returns:
            0.0-1.0 之间的重要性分数
        """
        # 显式标记优先
        if explicit_importance is not None:
            return max(0.0, min(1.0, explicit_importance))

        # 各维度评分
        role_score = self._score_role(agent_role)
        type_score = self._score_type(memory_type)
        quality_score = self._score_quality(content)
        decay_score = self._score_decay(created_at)
        access_score = self._score_access(access_count)

        # 加权综合
        final = (
            self.dim_weights["role"] * role_score
            + self.dim_weights["type"] * type_score
            + self.dim_weights["quality"] * quality_score
            + self.dim_weights["decay"] * decay_score
            + self.dim_weights["access"] * access_score
        )

        return round(max(0.0, min(1.0, final)), 4)

    # ========== 批量评分 ==========

    def score_batch(
        self,
        items: list[dict],
    ) -> list[float]:
        """
        批量评分

        Args:
            items: [{"content": ..., "agent_role": ..., "memory_type": ..., "created_at": ..., "access_count": ...}]

        Returns:
            对应的分数列表
        """
        return [
            self.score(
                content=item.get("content", ""),
                agent_role=item.get("agent_role", ""),
                memory_type=item.get("memory_type", "task"),
                created_at=item.get("created_at"),
                access_count=item.get("access_count", 0),
                explicit_importance=item.get("explicit_importance"),
            )
            for item in items
        ]
