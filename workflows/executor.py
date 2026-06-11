"""
工作流执行器 - 借鉴 ChatDev 的执行引擎

核心设计：
- 按拓扑排序执行节点
- 同层节点并行执行
- 支持条件路由和上下文传递
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from models.message import Message
from models.workflow import NodeType
from workflows.graph import WorkflowGraph

logger = logging.getLogger(__name__)


class NodeExecutor:
    """节点执行器基类"""

    async def execute(
        self,
        node_id: str,
        node_config: dict,
        input_message: Message,
        context: dict[str, Any],
    ) -> Message:
        """
        执行节点

        Args:
            node_id: 节点 ID
            node_config: 节点配置
            input_message: 输入消息
            context: 执行上下文

        Returns:
            输出消息
        """
        raise NotImplementedError


class AgentNodeExecutor(NodeExecutor):
    """Agent 节点执行器"""

    def __init__(self, agents: dict[str, Any], graph=None):
        self.agents = agents  # role -> agent instance
        self.graph = graph  # 工作流图（用于获取节点的 role）

    async def execute(
        self,
        node_id: str,
        node_config: dict,
        input_message: Message,
        context: dict[str, Any],
    ) -> Message:
        # 从图中获取节点的 role
        role = None
        if self.graph:
            node = self.graph.get_node(node_id)
            if node:
                role = node.role

        # 如果图中没有，尝试从 config 中获取（兼容旧配置）
        if not role:
            role = node_config.get("role")

        # 如果还是没有，尝试使用 node_id 作为 role（向后兼容）
        if not role:
            role = node_id

        if role not in self.agents:
            raise ValueError(f"Agent not found for role: {role}")

        agent = self.agents[role]
        result = await agent.run(input_message)

        if result is None:
            # Agent 没有输出，创建一个默认消息
            result = Message(
                content=f"[{node_id}] 无输出",
                cause_by=node_id,
                sent_from=node_id,
            )

        return result


class PassthroughNodeExecutor(NodeExecutor):
    """透传节点执行器"""

    async def execute(
        self,
        node_id: str,
        node_config: dict,
        input_message: Message,
        context: dict[str, Any],
    ) -> Message:
        return Message(
            content=input_message.content,
            cause_by=node_id,
            sent_from=node_id,
        )


class WorkflowExecutor:
    """
    工作流执行器

    借鉴 ChatDev 的执行引擎：
    - 按拓扑排序执行节点
    - 同层节点并行执行
    - 收集最终输出
    """

    def __init__(
        self,
        graph: WorkflowGraph,
        agents: Optional[Dict[str, Any]] = None,
    ):
        self.graph = graph
        self.agents = agents or {}

        # 注册默认执行器
        self.executors: Dict[str, NodeExecutor] = {
            NodeType.AGENT.value: AgentNodeExecutor(self.agents, self.graph),
            NodeType.PASSTHROUGH.value: PassthroughNodeExecutor(),
        }

    def register_executor(self, node_type: str, executor: NodeExecutor) -> None:
        """注册节点执行器"""
        self.executors[node_type] = executor

    async def execute(
        self,
        input_message: Message,
        max_parallel: int = 5,
    ) -> Dict[str, Any]:
        """
        执行工作流

        Args:
            input_message: 输入消息
            max_parallel: 最大并行数

        Returns:
            执行上下文（包含所有节点的输出）
        """
        logger.info(f"Starting workflow execution: {self.graph.id}")

        # 拓扑排序
        layers = self.graph.topological_sort()
        logger.info(f"Topology layers: {layers}")

        # 执行上下文
        context: Dict[str, Any] = {
            "input": input_message.to_dict(),
            "outputs": {},  # node_id -> output message
        }

        # 按层执行
        for layer_idx, layer in enumerate(layers):
            logger.info(f"Executing layer {layer_idx}: {layer}")

            # 并行执行当前层的节点
            tasks = []
            for node_id in layer:
                task = self._execute_node(node_id, input_message, context)
                tasks.append((node_id, task))

            # 等待当前层所有节点完成
            results = await asyncio.gather(
                *[task for _, task in tasks],
                return_exceptions=True,
            )

            # 处理结果
            for (node_id, _), result in zip(tasks, results):
                if isinstance(result, Exception):
                    logger.error(f"Node {node_id} failed: {result}")
                    context["outputs"][node_id] = {
                        "error": str(result),
                    }
                else:
                    context["outputs"][node_id] = result.to_dict()
                    # 更新输入消息为当前节点的输出（供下一层使用）
                    input_message = result

            logger.info(f"Layer {layer_idx} completed")

        # 收集最终输出
        final_output = self._collect_final_output(context)
        context["final_output"] = final_output

        logger.info(f"Workflow execution completed: {self.graph.id}")
        return context

    async def _execute_node(
        self,
        node_id: str,
        input_message: Message,
        context: Dict[str, Any],
    ) -> Message:
        """执行单个节点"""
        node = self.graph.get_node(node_id)
        if not node:
            raise ValueError(f"Node not found: {node_id}")

        # 获取执行器
        executor = self.executors.get(node.type.value)
        if not executor:
            raise ValueError(f"No executor for node type: {node.type.value}")

        # 执行节点
        logger.info(f"Executing node: {node_id} (type={node.type.value})")
        result = await executor.execute(
            node_id=node_id,
            node_config=node.config,
            input_message=input_message,
            context=context,
        )

        return result

    def _collect_final_output(self, context: Dict[str, Any]) -> Dict:
        """收集最终输出"""
        final_output = {}

        for node_id in self.graph.end_nodes:
            if node_id in context["outputs"]:
                final_output[node_id] = context["outputs"][node_id]

        # 如果没有指定终止节点，取最后一个输出
        if not final_output and context["outputs"]:
            last_key = list(context["outputs"].keys())[-1]
            final_output[last_key] = context["outputs"][last_key]

        return final_output
