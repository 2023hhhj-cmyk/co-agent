"""
Plan → Graph 转换器

把 CEO Agent 的 ProjectPlan 转成 WorkflowExecutor 能执行的 GraphDef。
每个 Task 变成一个 Node，每个 dependency 变成一条 Edge。

用法：
    converter = PlanToGraphConverter()
    graph_def = converter.convert(plan)
    graph = WorkflowGraph(graph_def)
    executor = WorkflowExecutor(graph, agents)
    result = await executor.execute(input_message)
"""

import logging
from typing import Optional

from models.task import ProjectPlan
from models.workflow import EdgeDef, GraphDef, NodeDef, NodeType

logger = logging.getLogger(__name__)


class PlanToGraphConverter:
    """把 ProjectPlan 转成 GraphDef（动态 DAG）"""

    def convert(self, plan: ProjectPlan) -> GraphDef:
        """
        转换 ProjectPlan 为 GraphDef

        Args:
            plan: CEO 拆解的项目计划

        Returns:
            GraphDef 实例，可直接交给 WorkflowExecutor 执行
        """
        # 构建 title → id 映射（LLM 可能在 dependencies 里用标题而非 id）
        title_to_id = {task.title: task.id for task in plan.tasks}

        # 1. 每个 Task → 一个 Node
        nodes = []
        for task in plan.tasks:
            nodes.append(NodeDef(
                id=task.id,
                type=NodeType.AGENT,
                role=task.assigned_to,
                description=task.description,
                config={
                    "task_title": task.title,
                    "priority": task.priority,
                },
            ))

        # 2. 每个 dependency → 一条 Edge（自动处理标题 → id 转换）
        edges = []
        skipped = []
        for task in plan.tasks:
            for dep in task.dependencies:
                # 尝试直接用 dep 作为 id
                source_id = dep
                # 如果 dep 不是已知 task id，尝试按标题匹配
                if dep not in {t.id for t in plan.tasks}:
                    source_id = title_to_id.get(dep)
                    if not source_id:
                        # 模糊匹配：dep 是某个标题的子串
                        for title, tid in title_to_id.items():
                            if dep in title or title in dep:
                                source_id = tid
                                break

                if source_id and source_id != task.id:
                    edges.append(EdgeDef(
                        source=source_id,
                        target=task.id,
                        description=f"{source_id} → {task.id}",
                    ))
                elif not source_id:
                    skipped.append(dep)

        if skipped:
            logger.warning(f"无法解析的依赖（已跳过）: {skipped}")

        # 3. 计算起止节点
        has_dependency = set()   # 有入边的节点
        is_dependency_of = set() # 有出边的节点
        for edge in edges:
            has_dependency.add(edge.target)
            is_dependency_of.add(edge.source)

        start_nodes = [t.id for t in plan.tasks if t.id not in has_dependency]
        end_nodes = [t.id for t in plan.tasks if t.id not in is_dependency_of]

        graph_def = GraphDef(
            id=f"plan_{plan.id}",
            description=plan.requirement[:200],
            nodes=nodes,
            edges=edges,
            start_nodes=start_nodes,
            end_nodes=end_nodes,
        )

        logger.info(
            f"Plan converted to DAG: {len(nodes)} nodes, {len(edges)} edges, "
            f"start={start_nodes}, end={end_nodes}"
        )

        return graph_def
