"""
工作流执行器 - 借鉴 ChatDev 的执行引擎

核心设计：
- 按拓扑排序执行节点
- 同层节点并行执行
- 支持条件路由和上下文传递
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from acceleration.limits import get_workspace_task_slots
from models.message import Message
from models.workflow import NodeType
from workflows.graph import WorkflowGraph

try:
    from analytics import emit_event  # 埋点（运行数据采集）
except Exception:  # noqa: BLE001
    def emit_event(*_a, **_kw):  # type: ignore[no-redef]
        return None

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
            logger.error(f"DEBUG AgentNodeExecutor: role={role!r} not in self.agents keys={list(self.agents.keys())}")
            raise ValueError(f"Agent not found for role: {role}")
        logger.info(f"DEBUG AgentNodeExecutor: node={node_id} role={role} agent={type(self.agents[role]).__name__}")

        base_agent = self.agents[role]
        # 每个任务一个执行上下文：共享 LLM/工具/记忆服务，隔离队列与工作记忆
        agent = (
            base_agent.fork(context.get("client_id", ""))
            if hasattr(base_agent, "fork") else base_agent
        )

        # 优先使用 CEO 拆解的任务描述（动态 DAG 场景）
        task_desc = context.get("task_descriptions", {}).get(node_id)
        if task_desc:
            task_metadata = context.get("task_metadata", {}).get(node_id, {})
            message = Message(
                content=task_desc,
                cause_by="ceo_task_assignment",
                sent_from="ceo",
                send_to={role},
                metadata=task_metadata,
            )
        else:
            message = input_message

        result = await agent.run(message)

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
        on_progress: Any = None,
    ):
        self.graph = graph
        self.agents = agents or {}
        self.on_progress = on_progress  # 进度回调：async def on_progress(node_id, result)

        # 注册默认执行器
        self.executors: Dict[str, NodeExecutor] = {
            NodeType.AGENT.value: AgentNodeExecutor(self.agents, self.graph),
            NodeType.PASSTHROUGH.value: PassthroughNodeExecutor(),
        }
        self._run_id = ""
        self._run_started = 0.0
        self._run_node_count = 0
        self._run_closed = False

    def register_executor(self, node_type: str, executor: NodeExecutor) -> None:
        """注册节点执行器"""
        self.executors[node_type] = executor

    async def execute(
        self,
        input_message: Message,
        max_parallel: int = 5,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        执行工作流

        Args:
            input_message: 输入消息
            max_parallel: 本工作流并发上限，同时受工作区共享上限约束
            extra_context: 额外上下文（如 CEO 拆解的任务描述和元数据）

        Returns:
            执行上下文（包含所有节点的输出）
        """
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        local_slots = asyncio.Semaphore(max_parallel)

        async def run_node(node_id, message, context, **trace):
            # Acquire local capacity first, so local waiters do not hold workspace slots.
            async with local_slots:
                async with get_workspace_task_slots():
                    return await self._execute_node_traced(node_id, message, context, **trace)

        logger.info(f"Starting workflow execution: {self.graph.id}")

        # 拓扑排序
        layers = self.graph.topological_sort()
        logger.info(f"Topology layers: {layers}")

        # 埋点：workflow.start
        _workflow_id = getattr(self.graph, "id", None) or "wf-unknown"
        _node_count = sum(len(layer) for layer in layers)
        _topo_depth = len(layers)
        emit_event("workflow.start", workflow_id=_workflow_id,
                   node_count=_node_count, topo_depth=_topo_depth)
        _wf_start = time.time()

        # 执行上下文
        context: Dict[str, Any] = {
            "input": input_message.to_dict(),
            "outputs": {},  # node_id -> output message
        }

        # 合并额外上下文（CEO 拆解的任务描述等）
        if extra_context:
            context.update(extra_context)

        try:
            # 按层执行
            for layer_idx, layer in enumerate(layers):
                logger.info(f"Executing layer {layer_idx}: {layer}")

                # 并行执行当前层的节点
                _parallel_count = len(layer)
                tasks = []
                for node_id in layer:
                    blocked_by = [
                        parent for parent in self.graph.reverse_adj[node_id]
                        if "error" in context["outputs"].get(parent, {})
                    ]
                    if blocked_by:
                        reason = f"前置任务失败或被跳过: {', '.join(blocked_by)}"
                        context["outputs"][node_id] = {
                            "error": reason,
                            "skipped": True,
                            "blocked_by": blocked_by,
                        }
                        logger.warning(f"Node {node_id} skipped: {reason}")
                        continue
                    task = run_node(
                        node_id, input_message, context,
                        depth=layer_idx, parallel_count=_parallel_count,
                        workflow_id=_workflow_id,
                    )
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
                        node_out = result.to_dict()
                        # 返回错误 Message 而未抛异常的节点，同样记为失败
                        if getattr(result, "cause_by", "") in ("error", "failed"):
                            node_out["error"] = (result.content or "节点返回失败")[:200]
                        context["outputs"][node_id] = node_out
                        # 更新输入消息为当前节点的输出（供下一层使用）
                        input_message = result

                        # 进度回调
                        if self.on_progress:
                            try:
                                await self.on_progress(node_id, result)
                            except Exception as e:
                                logger.warning(f"Progress callback failed for {node_id}: {e}")

                logger.info(f"Layer {layer_idx} completed")

            # 收集最终输出
            final_output = self._collect_final_output(context)
            context["final_output"] = final_output

            # 埋点：workflow.end（成功路径）
            _total = _node_count
            _success_n = sum(
                1 for v in context["outputs"].values()
                if not (isinstance(v, dict) and "error" in v)
            )
            if _success_n == _total and _total > 0:
                _status = "success"
            elif _success_n == 0:
                _status = "fail"
            else:
                _status = "partial"
            emit_event("workflow.end", workflow_id=_workflow_id, status=_status,
                       total_nodes=_total, success_nodes=_success_n,
                       duration_s=round(time.time() - _wf_start, 3))

            logger.info(f"Workflow execution completed: {self.graph.id}")
            return context
        except Exception:
            # 失败也要埋点 workflow.end
            emit_event("workflow.end", workflow_id=_workflow_id, status="fail",
                       total_nodes=_node_count, success_nodes=0,
                       duration_s=round(time.time() - _wf_start, 3))
            raise

    def open_run(self) -> None:
        """CEO 开始控制一轮执行时记一次 workflow.start。"""
        layers = self.graph.topological_sort()
        self._run_id = getattr(self.graph, "id", None) or "wf-unknown"
        self._run_node_count = sum(len(layer) for layer in layers)
        self._run_started = time.time()
        self._run_closed = False
        emit_event(
            "workflow.start",
            workflow_id=self._run_id,
            node_count=self._run_node_count,
            topo_depth=len(layers),
        )

    def close_run(self, context: Dict[str, Any], failed: bool = False) -> None:
        """整轮结束时记一次 workflow.end。重复调用不再发事件。"""
        if self._run_closed:
            return
        self._run_closed = True
        if failed:
            status, success_n = "fail", 0
        else:
            success_n = sum(
                1 for value in context.get("outputs", {}).values()
                if not (isinstance(value, dict) and "error" in value)
            )
            total = self._run_node_count
            if success_n == total and total > 0:
                status = "success"
            elif success_n == 0:
                status = "fail"
            else:
                status = "partial"
        emit_event(
            "workflow.end",
            workflow_id=self._run_id or "wf-unknown",
            status=status,
            total_nodes=self._run_node_count,
            success_nodes=success_n,
            duration_s=round(time.time() - self._run_started, 3),
        )

    async def dispatch(
        self,
        node_ids: List[str],
        input_message: Message,
        context: Dict[str, Any],
        max_parallel: int = 5,
        depth: int = 0,
    ) -> None:
        """只执行 CEO 点名的节点，把结果写回 context。不决定下一批任务。"""
        if not node_ids:
            return
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        local_slots = asyncio.Semaphore(max_parallel)
        workflow_id = self._run_id or (getattr(self.graph, "id", None) or "wf-unknown")

        async def run_node(node_id: str) -> Message:
            async with local_slots:
                async with get_workspace_task_slots():
                    return await self._execute_node_traced(
                        node_id,
                        input_message,
                        context,
                        depth=depth,
                        parallel_count=len(node_ids),
                        workflow_id=workflow_id,
                    )

        tasks = [(node_id, run_node(node_id)) for node_id in node_ids]
        results = await asyncio.gather(
            *[task for _, task in tasks],
            return_exceptions=True,
        )
        for (node_id, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.error(f"Node {node_id} failed: {result}")
                context["outputs"][node_id] = {"error": str(result)}
            else:
                context["outputs"][node_id] = result.to_dict()

    async def _execute_node_traced(
        self,
        node_id: str,
        input_message: Message,
        context: Dict[str, Any],
        depth: int,
        parallel_count: int,
        workflow_id: str,
    ) -> Message:
        """包一层埋点的节点执行器（保持原 _execute_node 逻辑不变）。"""
        node = self.graph.get_node(node_id)
        node_type = node.type.value if node else "unknown"
        emit_event("workflow.node", workflow_id=workflow_id, node_id=node_id,
                   node_type=node_type, depth=depth, parallel_count=parallel_count,
                   phase="start")
        _n_start = time.time()
        try:
            result = await self._execute_node(node_id, input_message, context)
            emit_event("workflow.node", workflow_id=workflow_id, node_id=node_id,
                       node_type=node_type, depth=depth, parallel_count=parallel_count,
                       phase="end", duration_ms=int((time.time() - _n_start) * 1000),
                       success=True)
            return result
        except Exception:
            emit_event("workflow.node", workflow_id=workflow_id, node_id=node_id,
                       node_type=node_type, depth=depth, parallel_count=parallel_count,
                       phase="end", duration_ms=int((time.time() - _n_start) * 1000),
                       success=False)
            raise

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
