"""
工作流图 - 借鉴 ChatDev 的 DAG 结构

核心设计：
- WorkflowGraph: DAG 工作流图
- 拓扑排序：Kahn 算法，支持并行执行
- 条件路由：根据上下文决定下一步
"""

import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set

from models.workflow import EdgeDef, GraphDef, NodeDef

logger = logging.getLogger(__name__)


class WorkflowGraph:
    """
    DAG 工作流图

    借鉴 ChatDev 的图结构：
    - 节点代表 Agent 或工具
    - 边代表消息传递路径
    - 支持拓扑排序和条件路由
    """

    def __init__(self, graph_def: GraphDef):
        self.graph_def = graph_def
        self.id = graph_def.id
        self.description = graph_def.description

        # 节点映射
        self.nodes: Dict[str, NodeDef] = {}
        for node in graph_def.nodes:
            self.nodes[node.id] = node

        # 邻接表（正向和反向）
        self.adj: Dict[str, List[str]] = defaultdict(list)
        self.reverse_adj: Dict[str, List[str]] = defaultdict(list)
        self.edges_map: Dict[tuple, EdgeDef] = {}

        for edge in graph_def.edges:
            self.adj[edge.source].append(edge.target)
            self.reverse_adj[edge.target].append(edge.source)
            self.edges_map[(edge.source, edge.target)] = edge

        # 起始和终止节点
        self.start_nodes = graph_def.start_nodes
        self.end_nodes = graph_def.end_nodes

        # 验证图结构
        self._validate()

    def _validate(self) -> None:
        """验证图结构"""
        errors = self.graph_def.validate()
        if errors:
            for error in errors:
                logger.error(f"Graph validation error: {error}")
            raise ValueError(f"Invalid graph structure: {errors}")

        # 检查是否有环
        if self._has_cycle():
            raise ValueError("Graph contains cycles")

    def _has_cycle(self) -> bool:
        """检测是否有环（DFS）"""
        visited = set()
        rec_stack = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in self.adj[node]:
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        for node in self.nodes:
            if node not in visited:
                if dfs(node):
                    return True
        return False

    def topological_sort(self) -> List[List[str]]:
        """
        拓扑排序（Kahn 算法）

        返回分层结果，同层节点可并行执行

        Returns:
            list of layers, each layer is a list of node IDs
        """
        # 计算入度
        in_degree = {node_id: 0 for node_id in self.nodes}
        for node_id in self.nodes:
            for neighbor in self.adj[node_id]:
                in_degree[neighbor] += 1

        # 初始化队列（入度为 0 的节点）
        queue = deque()
        for node_id, degree in in_degree.items():
            if degree == 0:
                queue.append(node_id)

        layers = []
        while queue:
            # 当前层的所有节点
            layer = list(queue)
            layers.append(layer)

            # 处理当前层
            next_queue = deque()
            for node_id in layer:
                for neighbor in self.adj[node_id]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        next_queue.append(neighbor)

            queue = next_queue

        return layers

    def get_next_nodes(self, node_id: str, context: Optional[dict] = None) -> List[str]:
        """
        获取下一节点（支持条件路由）

        Args:
            node_id: 当前节点 ID
            context: 执行上下文（用于条件判断）

        Returns:
            下一节点 ID 列表
        """
        next_nodes = []
        for target in self.adj[node_id]:
            edge = self.edges_map.get((node_id, target))
            if edge and self._evaluate_condition(edge, context):
                next_nodes.append(target)
        return next_nodes

    def _evaluate_condition(self, edge: EdgeDef, context: Optional[dict] = None) -> bool:
        """
        评估边的条件

        Args:
            edge: 边定义
            context: 执行上下文

        Returns:
            条件是否满足
        """
        if not edge.condition:
            return True

        if not context:
            return True

        try:
            # 安全的条件评估
            # 只允许访问 context 中的变量
            return eval(edge.condition, {"__builtins__": {}}, context)
        except Exception as e:
            logger.warning(f"Condition evaluation failed for edge {edge.source}->{edge.target}: {e}")
            return False

    def get_node(self, node_id: str) -> Optional[NodeDef]:
        """获取节点定义"""
        return self.nodes.get(node_id)

    def get_edge(self, source: str, target: str) -> Optional[EdgeDef]:
        """获取边定义"""
        return self.edges_map.get((source, target))

    def get_predecessors(self, node_id: str) -> List[str]:
        """获取前驱节点"""
        return self.reverse_adj[node_id]

    def get_successors(self, node_id: str) -> List[str]:
        """获取后继节点"""
        return self.adj[node_id]

    def is_start_node(self, node_id: str) -> bool:
        """是否为起始节点"""
        return node_id in self.start_nodes

    def is_end_node(self, node_id: str) -> bool:
        """是否为终止节点"""
        return node_id in self.end_nodes

    def to_dict(self) -> dict:
        """序列化为字典"""
        return self.graph_def.to_dict()
