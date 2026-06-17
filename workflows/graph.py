"""
工作流图 - 借鉴 ChatDev 的 DAG 结构

核心设计：
- WorkflowGraph: DAG 工作流图
- 拓扑排序：Kahn 算法，支持并行执行
- 条件路由：根据上下文决定下一步
"""

import ast
import logging
import operator
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set

from models.workflow import EdgeDef, GraphDef, NodeDef

logger = logging.getLogger(__name__)

# ========== 安全条件求值器 ==========

# 白名单：只允许以下 AST 节点类型和操作符
_SAFE_COMPARE_OPS: dict = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_SAFE_BOOL_OPS: dict = {
    ast.And: lambda a, b: a and b,
    ast.Or: lambda a, b: a or b,
}

_SAFE_BUILTINS: dict = {
    "True": True,
    "False": False,
    "None": None,
    "str": str,
    "int": int,
    "bool": bool,
    "len": len,
}


def _safe_eval_condition(condition_str: str, context: dict) -> bool:
    """
    使用 AST 白名单安全求值布尔表达式

    只允许：
    - 比较操作：==, !=, <, >, <=, >=, is, is not, in, not in
    - 布尔操作：and, or, not
    - 字面量：True, False, None, 数字, 字符串
    - 函数调用：仅限 context.get(key, default) 和 str(), int(), bool(), len()
    - 变量名：仅限 _SAFE_BUILTINS 中的内置名称

    禁止：属性访问链（如 obj.attr.method）、import、exec、__ 开头的名称等
    """
    tree = ast.parse(condition_str.strip(), mode="eval")
    result = _eval_node(tree.body, context)
    return bool(result)


def _eval_node(node, context: dict):
    """递归求值 AST 节点"""

    # --- 布尔操作: and / or ---
    if isinstance(node, ast.BoolOp):
        op_func = _SAFE_BOOL_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"不支持的布尔操作符: {type(node.op).__name__}")
        result = _eval_node(node.values[0], context)
        for val in node.values[1:]:
            result = op_func(result, _eval_node(val, context))
        return result

    # --- 比较操作: ==, !=, <, >, <=, >=, is, in 等 ---
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op_obj, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, context)
            op_func = _SAFE_COMPARE_OPS.get(type(op_obj))
            if op_func is None:
                raise ValueError(f"不支持的比较操作符: {type(op_obj).__name__}")
            if not op_func(left, right):
                return False
            left = right
        return True

    # --- 一元操作: not ---
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, context)

    # --- 字面量常量 ---
    if isinstance(node, ast.Constant):
        return node.value

    # --- 变量名 ---
    if isinstance(node, ast.Name):
        if node.id in _SAFE_BUILTINS:
            return _SAFE_BUILTINS[node.id]
        raise ValueError(f"不允许的变量名: {node.id}")

    # --- 函数调用: 仅 context.get(key, default) ---
    if isinstance(node, ast.Call):
        # 允许 context.get(...)
        if (isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "context"
                and node.func.attr == "get"):
            key = _eval_node(node.args[0], context)
            # 阻止访问以 __ 开头的 key（防止沙箱逃逸）
            if isinstance(key, str) and key.startswith("__"):
                raise ValueError(f"不允许访问以 __ 开头的 key: {key}")
            default = _eval_node(node.args[1], context) if len(node.args) > 1 else None
            return context.get(key, default)
        # 允许内置安全函数: str(), int(), bool(), len()
        if isinstance(node.func, ast.Name) and node.func.id in _SAFE_BUILTINS:
            if not node.args:
                raise ValueError(f"{node.func.id}() 需要至少一个参数")
            arg = _eval_node(node.args[0], context)
            return _SAFE_BUILTINS[node.func.id](arg)
        raise ValueError(f"不允许的函数调用: {ast.dump(node.func)}")

    # --- 属性访问: 禁止（context.get 已在上面的 Call 中处理） ---
    if isinstance(node, ast.Attribute):
        raise ValueError(f"不允许的属性访问: {node.attr}")

    # --- 禁止所有其他节点类型 ---
    raise ValueError(f"不支持的表达式类型: {type(node).__name__}")


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
        评估边的条件（使用 AST 白名单安全求值）

        只允许简单的布尔表达式，如：
          context.get("key") == "value"
          context.get("count", 0) > 5 and context.get("status") == "ok"

        禁止：属性访问、函数调用（context.get 除外）、任何可能引起代码执行的操作

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
            return _safe_eval_condition(edge.condition, context)
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
