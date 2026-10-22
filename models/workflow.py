"""
工作流图定义模型 - 借鉴 ChatDev 的图结构

核心设计：
- NodeDef: 节点定义（以 Agent 为核心执行单元）
- EdgeDef: 有向边定义（支持条件路由）
- GraphDef: DAG 图定义
- WorkflowDef: 完整工作流定义（含全局变量）
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class NodeType(str, Enum):
    """节点类型（当前实际业务运行均为 AGENT，TOOL 等仅作为通用引擎底层预留）"""
    AGENT = "agent"  # LLM Agent 节点（核心使用，工具由 Agent 内部自治调用）
    TOOL = "tool"  # 工具调用节点（底层引擎预留扩展，当前业务流中不作为独立节点）
    HUMAN = "human"  # 人类参与节点（预留扩展）
    PYTHON = "python_runner"  # Python 执行节点（预留扩展）
    SUBGRAPH = "subgraph"  # 子图节点（预留扩展）
    PASSTHROUGH = "passthrough"  # 透传节点


class NodeDef(BaseModel):
    """
    节点定义

    每个节点代表工作流中的一个执行单元
    """
    id: str  # 节点唯一标识
    type: NodeType = NodeType.AGENT  # 节点类型
    role: Optional[str] = None  # Agent 角色名称（type=agent 时使用）
    description: str = ""  # 节点描述
    config: dict = Field(default_factory=dict)  # 节点配置

    # 执行配置
    timeout: int = 300  # 超时时间（秒）
    retry: int = 0  # 重试次数

    def to_dict(self) -> dict:
        data = self.model_dump()
        data["type"] = data["type"].value if isinstance(data["type"], NodeType) else data["type"]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "NodeDef":
        return cls(**data)


class EdgeDef(BaseModel):
    """
    有向边定义

    连接两个节点，支持条件路由
    """
    source: str  # 源节点 ID
    target: str  # 目标节点 ID（支持单个或多个）
    condition: Optional[str] = None  # 条件表达式（可选）
    description: str = ""  # 边描述

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "EdgeDef":
        return cls(**data)


class GraphDef(BaseModel):
    """
    DAG 图定义

    借鉴 ChatDev 的图结构：
    - 节点代表 Agent 执行角色（工具由各 Agent 内部自治调用，不作为独立图节点）
    - 边代表消息传递路径
    - 支持条件路由和并行执行
    """
    id: str  # 图唯一标识
    description: str = ""  # 图描述
    nodes: list[NodeDef] = Field(default_factory=list)  # 节点列表
    edges: list[EdgeDef] = Field(default_factory=list)  # 边列表
    start_nodes: list[str] = Field(default_factory=list)  # 起始节点
    end_nodes: list[str] = Field(default_factory=list)  # 终止节点

    def get_node(self, node_id: str) -> Optional[NodeDef]:
        """根据 ID 获取节点"""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_edges_from(self, node_id: str) -> list[EdgeDef]:
        """获取从指定节点出发的所有边"""
        return [edge for edge in self.edges if edge.source == node_id]

    def get_edges_to(self, node_id: str) -> list[EdgeDef]:
        """获取指向指定节点的所有边"""
        return [edge for edge in self.edges if edge.target == node_id]

    def validate(self) -> list[str]:
        """验证图结构，返回错误列表"""
        errors = []
        node_ids = {node.id for node in self.nodes}

        # 检查节点 ID 唯一性
        seen = set()
        for node in self.nodes:
            if node.id in seen:
                errors.append(f"重复的节点 ID: {node.id}")
            seen.add(node.id)

        # 检查起始节点是否存在
        for node_id in self.start_nodes:
            if node_id not in node_ids:
                errors.append(f"起始节点不存在: {node_id}")

        # 检查终止节点是否存在
        for node_id in self.end_nodes:
            if node_id not in node_ids:
                errors.append(f"终止节点不存在: {node_id}")

        # 检查边的节点是否存在
        for edge in self.edges:
            if edge.source not in node_ids:
                errors.append(f"边的源节点不存在: {edge.source}")
            if edge.target not in node_ids:
                errors.append(f"边的目标节点不存在: {edge.target}")

        return errors

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "GraphDef":
        return cls(**data)


class WorkflowDef(BaseModel):
    """
    完整工作流定义

    包含图定义和全局配置
    """
    version: str = "1.0"  # 版本号
    graph: GraphDef  # 图定义
    vars: dict = Field(default_factory=dict)  # 全局变量

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowDef":
        return cls(**data)
