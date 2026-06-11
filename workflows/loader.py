"""
工作流加载器 - YAML 配置加载

支持从 YAML 文件加载工作流定义
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Union

import yaml

from models.workflow import EdgeDef, GraphDef, NodeDef, NodeType, WorkflowDef

logger = logging.getLogger(__name__)


class WorkflowLoader:
    """工作流加载器"""

    def load(self, yaml_path: Union[str, Path]) -> WorkflowDef:
        """
        从 YAML 文件加载工作流定义

        Args:
            yaml_path: YAML 文件路径

        Returns:
            WorkflowDef 实例
        """
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Workflow file not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return self.load_from_dict(data)

    def load_from_dict(self, data: Dict[str, Any]) -> WorkflowDef:
        """
        从字典加载工作流定义

        Args:
            data: 工作流配置字典

        Returns:
            WorkflowDef 实例
        """
        # 解析图定义
        graph_data = data.get("graph", {})
        graph_def = self._parse_graph(graph_data)

        return WorkflowDef(
            version=data.get("version", "1.0"),
            graph=graph_def,
            vars=data.get("vars", {}),
        )

    def _parse_graph(self, data: Dict[str, Any]) -> GraphDef:
        """解析图定义"""
        # 解析节点
        nodes = []
        for node_data in data.get("nodes", []):
            node = self._parse_node(node_data)
            nodes.append(node)

        # 解析边
        edges = []
        for edge_data in data.get("edges", []):
            edge_list = self._parse_edge(edge_data)
            edges.extend(edge_list)

        return GraphDef(
            id=data.get("id", "default"),
            description=data.get("description", ""),
            nodes=nodes,
            edges=edges,
            start_nodes=data.get("start_nodes", []),
            end_nodes=data.get("end_nodes", []),
        )

    def _parse_node(self, data: Dict[str, Any]) -> NodeDef:
        """解析节点定义"""
        # 解析节点类型
        node_type_str = data.get("type", "agent")
        try:
            node_type = NodeType(node_type_str)
        except ValueError:
            logger.warning(f"Unknown node type: {node_type_str}, defaulting to agent")
            node_type = NodeType.AGENT

        return NodeDef(
            id=data["id"],
            type=node_type,
            role=data.get("role"),
            description=data.get("description", ""),
            config=data.get("config", {}),
            timeout=data.get("timeout", 300),
            retry=data.get("retry", 0),
        )

    def _parse_edge(self, data: Dict[str, Any]) -> List[EdgeDef]:
        """
        解析边定义

        支持 target 为单个字符串或列表（自动展开为多条边）
        """
        source = data["source"]
        target = data.get("target")
        condition = data.get("condition")
        description = data.get("description", "")

        if target is None:
            raise ValueError(f"Edge target is required: {data}")

        edges = []

        if isinstance(target, list):
            # target 是列表，展开为多条边
            for t in target:
                edges.append(EdgeDef(
                    source=source,
                    target=t,
                    condition=condition,
                    description=description,
                ))
        else:
            # target 是单个字符串
            edges.append(EdgeDef(
                source=source,
                target=target,
                condition=condition,
                description=description,
            ))

        return edges
