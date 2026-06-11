"""
Agent 角色定义模型 - 借鉴 MetaGPT 的 Role 设计

核心设计：
- AgentRole: 角色定义（name, profile, goal, constraints）
- ReactMode: 反应模式（REACT / BY_ORDER / PLAN_AND_ACT）
- Action: 动作定义
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class ReactMode(str, Enum):
    """
    Agent 反应模式（借鉴 MetaGPT）

    - REACT: LLM 动态选择下一步动作（灵活，适合复杂决策）
    - BY_ORDER: 按顺序执行动作列表（确定性，适合固定流程）
    - PLAN_AND_ACT: 先规划再执行（适合复杂任务分解）
    """
    REACT = "react"
    BY_ORDER = "by_order"
    PLAN_AND_ACT = "plan_and_act"


class ActionDef(BaseModel):
    """
    动作定义

    描述 Agent 可执行的一个动作单元
    """
    name: str  # 动作名称
    description: str = ""  # 动作描述
    prompt_template: str = ""  # 提示词模板
    output_schema: Optional[dict] = None  # 输出结构化 schema

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "ActionDef":
        return cls(**data)


class AgentRole(BaseModel):
    """
    Agent 角色定义（借鉴 MetaGPT Role）

    核心字段：
    - name: 角色名称
    - profile: 职位描述
    - goal: 目标
    - constraints: 约束条件
    - tools: 可用工具列表
    - actions: 可执行动作列表
    - watch: 订阅的消息来源类型（cause_by）
    - react_mode: 反应模式
    """
    name: str
    profile: str = ""  # 职位描述
    goal: str = ""  # 目标
    constraints: str = ""  # 约束条件
    desc: str = ""  # 详细描述

    # 工具和动作
    tools: list[str] = Field(default_factory=list)  # 可用工具列表
    actions: list[ActionDef] = Field(default_factory=list)  # 可执行动作

    # 消息订阅（MetaGPT 核心机制）
    watch: set[str] = Field(default_factory=set)  # 订阅的 cause_by 类型

    # 反应模式
    react_mode: ReactMode = ReactMode.REACT
    max_react_loop: int = 10  # REACT 模式最大循环次数

    def get_system_prompt(self) -> str:
        """生成系统提示词"""
        parts = []
        if self.profile:
            parts.append(f"你是{self.profile}。")
        if self.goal:
            parts.append(f"你的目标是：{self.goal}")
        if self.constraints:
            parts.append(f"约束条件：{self.constraints}")
        if self.tools:
            parts.append(f"可用工具：{', '.join(self.tools)}")
        return "\n".join(parts)

    def get_action_names(self) -> list[str]:
        """获取所有动作名称"""
        return [action.name for action in self.actions]

    def get_action_by_name(self, name: str) -> Optional["ActionDef"]:
        """根据名称获取动作"""
        for action in self.actions:
            if action.name == name:
                return action
        return None

    def to_dict(self) -> dict:
        data = self.model_dump()
        data["watch"] = list(data["watch"])  # set -> list
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRole":
        if "watch" in data and isinstance(data["watch"], list):
            data["watch"] = set(data["watch"])
        return cls(**data)
