"""
开发工程师 Agent

职责：
1. 根据客户需求，在服务器上为客户创建专属项目目录
2. 调用 Claude Code 构建实际产品（网站、应用等）
3. 生成成果摘要文档，交付给下游 Agent

工作流程（v3）：
- 收到客户 PRD → SubTaskController 让 LLM 评估 → 拆分子任务
- 拓扑排序 → 分层（DAG）→ 每层并行执行
- 子任务含状态机（PENDING/READY/RUNNING/.../SKIPPED）
- LLM 总控决策 retry/skip/abort
- 全部完成后整合
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

from agents.base import BaseAgent
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message
from models.task import SubTask, SubTaskStatus
from tools.code.claude_code import ClaudeCodeTool, ClaudeCodeResult
from tools.llm.client import LLMClient
from tools.llm.prompt import PromptManager

logger = logging.getLogger(__name__)

# 开发工程师角色定义
DEVELOPER_ROLE = AgentRole(
    name="developer",
    profile="资深开发工程师",
    goal="根据客户需求构建完整的产品，生成成果摘要文档",
    constraints="代码需要清晰、可维护，最终交付摘要文档而非代码",
    tools=["code_editor", "git", "testing_framework"],
    actions=[
        ActionDef(
            name="design_architecture",
            description="设计技术架构",
            prompt_template="请根据以下PRD设计技术架构：\n{prd_content}",
        ),
        ActionDef(
            name="implement_feature",
            description="实现功能代码",
            prompt_template="请根据以下技术方案实现代码：\n{architecture}\n\nPRD要求：\n{prd_content}",
        ),
        ActionDef(
            name="review_code",
            description="审查代码质量",
            prompt_template="请审查以下代码：\n{code}",
        ),
    ],
    watch={"ceo_task_assignment", "create_prd", "architecture_design", "bug_report"},
    react_mode=ReactMode.REACT,
    max_react_loop=5,
)


class DeveloperAgent(BaseAgent):
    """开发工程师 Agent"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        message_bus: Any = None,
        base_dir: str = "/root",
        memory_manager: Any = None,
        **kwargs,
    ):
        super().__init__(
            role=DEVELOPER_ROLE,
            llm_client=llm_client,
            message_bus=message_bus,
            memory_manager=memory_manager,
            **kwargs,
        )
        self.base_dir = base_dir
        self.claude_code = ClaudeCodeTool(
            base_dir=base_dir,
            timeout=900,  # 15 分钟超时
        )
        # 存储中间结果
        self._context: dict[str, str] = {}
        # 当前客户目录
        self._client_dir: Optional[str] = None

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """
        思考阶段：根据消息决定下一步动作

        收到PRD后，先设计架构，再实现代码
        """
        if not messages:
            return None

        last_message = messages[-1]

        if last_message.cause_by in ("create_prd", "ceo_task_assignment"):
            return self.role.get_action_by_name("design_architecture")
        elif last_message.cause_by == "architecture_design":
            return self.role.get_action_by_name("implement_feature")
        elif last_message.cause_by == "bug_report":
            return self.role.get_action_by_name("implement_feature")

        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """
        执行阶段：调用 Claude Code 执行动作

        完整流程：
        1. design_architecture: 创建客户目录，设计架构
        2. implement_feature: 调用 Claude Code 构建产品
        3. review_code: 审查代码，生成摘要文档
        """
        messages = context.get("messages", []) if context else []
        last_message = messages[-1] if messages else None

        if action.name == "design_architecture":
            return await self._design_architecture_v3(last_message)
        elif action.name == "implement_feature":
            return await self._implement_feature(last_message, messages)
        elif action.name == "review_code":
            return await self._review_code(last_message)
        else:
            return Message(
                content=f"未知动作：{action.name}",
                cause_by="error",
                sent_from=self.agent_id,
            )

    # ========== v3: SubTask 状态机 + DAG + LLM 总控 ==========

    async def _design_architecture_v3(self, last_message: Optional[Message]) -> Message:
        """ponytail: v3 主流程——判断拆分 → DAG → 状态机驱动执行 → 整合。"""
        prd = last_message.content if last_message else ""
        client_name = self._extract_client_name(prd)
        self._client_dir = self.claude_code.create_client_directory(client_name)
        os.makedirs(self._client_dir, exist_ok=True)
        self._context["prd"] = prd
        self._context["client_name"] = client_name

        # 1. 调 LLM 拆任务
        subtasks = await self._decompose_task(prd)
        logger.info(f"v3 拆出 {len(subtasks)} 个子任务: {[s.name for s in subtasks]}")

        if not subtasks:
            return Message(content="v3 拆解失败：未产出子任务", cause_by="error", sent_from=self.agent_id)

        # 2. 单子任务 → 跳过 DAG
        if len(subtasks) == 1:
            result = await self._run_with_state_machine(subtasks[0], {})
            self._context["architecture"] = result
            return self._build_architecture_message(result, client_name, self._client_dir)

        # 3. 拓扑排序 → 分层
        try:
            layers = self._topo_layers(subtasks)
        except ValueError as e:
            logger.error(f"DAG 拓扑失败: {e}")
            return Message(content=f"DAG 有环: {e}", cause_by="error", sent_from=self.agent_id)

        all_subtasks = {s.name: s for layer in layers for s in layer}
        completed = {}

        # 4. 分层执行（每层并行，限流 3 个）
        for layer_idx, layer in enumerate(layers):
            logger.info(f"=== v3 执行第 {layer_idx+1}/{len(layers)} 层 {[s.name for s in layer]} ===")
            # LLM 总控：决定是否跳过
            decision = await self._controller.on_layer_start(layer, completed)
            if decision.get("action") == "abort":
                logger.warning("LLM 总控：终止")
                break
            skip = set(decision.get("skip", []))
            layer = [s for s in layer if s.name not in skip]
            if not layer:
                continue

            # 并行执行
            results = await asyncio.gather(
                *[self._run_with_limit(s, completed) for s in layer],
                return_exceptions=True,
            )
            # 标记下游失败为 SKIPPED
            for s, r in zip(layer, results):
                if isinstance(r, Exception) or (
                    hasattr(r, "status") and r.status == SubTaskStatus.FAILED
                ):
                    self._mark_downstream_skipped(s, all_subtasks)

        # 5. 整合
        final = await self._integrate_results(prd, completed)
        self._context["architecture"] = final
        return self._build_architecture_message(final, client_name, self._client_dir)

    async def _decompose_task(self, task: str) -> list[SubTask]:
        """ponytail: 调 Claude Code 评估 + 拆分子任务（输出严格 JSON）。"""
        prompt = (
            "评估下面任务，判断要不要拆成多个独立子任务。\n"
            "输出严格 JSON（不要 markdown code fence，不要额外文字）：\n"
            '{"subtasks": [\n'
            '  {"name": "<英文短名>", "desc": "<子任务描述>", "deps": ["<依赖的其他子任务名>"]}\n'
            "]}\n\n"
            f"任务：\n{task}"
        )
        try:
            result = await self.claude_code.execute(task=prompt, context="", working_dir=self._client_dir or "/tmp")
            if not result.success:
                logger.warning(f"Claude Code 拆解失败: {result.error}")
                return [SubTask(name="default", desc=task, deps=[])]
            raw = result.output
            # 容忍 markdown fence
            raw = re.sub(r"^```(?:json)?\s*\n", "", raw.strip())
            raw = re.sub(r"\n```\s*$", "", raw)
            data = json.loads(raw)
            return [SubTask(name=s["name"], desc=s["desc"], deps=s.get("deps", [])) for s in data["subtasks"]]
        except Exception as e:
            logger.warning(f"拆解失败降级: {e}")
            return [SubTask(name="default", desc=task, deps=[])]

    def _topo_layers(self, subtasks: list[SubTask]) -> list[list[SubTask]]:
        """ponytail: Kahn 拓扑排序，返回分层列表（每层可并行）。"""
        name_to_sub = {s.name: s for s in subtasks}
        in_degree = {s.name: len(s.deps) for s in subtasks}
        layers = []
        while in_degree:
            ready = [name_to_sub[n] for n in list(in_degree.keys()) if in_degree[n] == 0]
            if not ready:
                cycle = [n for n, d in in_degree.items() if d > 0]
                raise ValueError(f"DAG 有环: {cycle}")
            layers.append(ready)
            completed_names = {s.name for s in ready}
            for s in ready:
                del in_degree[s.name]
            for n in list(in_degree.keys()):
                sub = name_to_sub[n]
                in_degree[n] = sum(1 for d in sub.deps if d not in completed_names and d in name_to_sub)
        return layers

    async def _run_with_limit(self, sub: SubTask, completed: dict) -> SubTask:
        """ponytail: 限流 3 个并行 + 状态机执行。"""
        async with self._semaphore:
            return await self._run_with_state_machine(sub, completed)

    async def _run_with_state_machine(self, sub: SubTask, completed: dict) -> SubTask:
        """ponytail: 单子任务状态机驱动执行（重试由 LLM 总控决策）。"""
        # 转移：READY → RUNNING
        if SubTaskStatus.can_transition(sub.status, SubTaskStatus.RUNNING):
            sub.status = SubTaskStatus.RUNNING
        sub.start_time = time.time()
        try:
            result = await self._run_single_with_retry(sub)
            sub.status = SubTaskStatus.SUCCESS
            sub.output_path = self._save_subtask_output(sub, result)
            completed[sub.name] = sub.output_path
            sub.duration_ms = int((time.time() - sub.start_time) * 1000)
            return sub
        except Exception as e:
            sub.error = str(e)[:500]
            # LLM 总控决策
            decision = await self._controller.on_subtask_fail(sub, sub.error)
            action = decision.get("action", "abort")
            if action == "retry" and sub.retries < 3:
                sub.retries += 1
                sub.status = SubTaskStatus.RETRYING
                logger.info(f"子任务 {sub.name} 重试 #{sub.retries}")
                return await self._run_with_state_machine(sub, completed)
            elif action == "skip":
                sub.status = SubTaskStatus.SKIPPED
                return sub
            else:
                sub.status = SubTaskStatus.FAILED
                sub.duration_ms = int((time.time() - sub.start_time) * 1000)
                return sub

    async def _run_single_with_retry(self, sub: SubTask, max_retries: int = 3) -> str:
        """ponytail: 调 Claude Code 跑子任务，指数退避重试。"""
        deps_context = ""
        if sub.deps:
            deps_paths = [f"  - {d}: {completed.get(d, '(missing)')}" for d in sub.deps if d in completed] if hasattr(sub, '_completed_ref') else []
            deps_context = "\n依赖产物路径:\n" + "\n".join(deps_paths) if deps_paths else ""

        full_task = f"实现子任务 `{sub.name}`：{sub.desc}{deps_context}"
        last_err = None
        for attempt in range(max_retries):
            try:
                result = await self.claude_code.execute(
                    task=full_task, context="", working_dir=self._client_dir or "/tmp"
                )
                if result.success:
                    return result.output
                last_err = result.error
            except Exception as e:
                last_err = str(e)
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"子任务 {sub.name} 第 {attempt+1} 次失败，{wait}s 后重试: {last_err}")
                await asyncio.sleep(wait)
        raise RuntimeError(f"子任务 {sub.name} 重试 {max_retries} 次仍失败: {last_err}")

    def _save_subtask_output(self, sub: SubTask, content: str) -> str:
        """ponytail: 子任务产物写到 client_dir/subtasks/<name>.md。"""
        if not self._client_dir:
            return ""
        out_dir = os.path.join(self._client_dir, "subtasks")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{sub.name}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def _mark_downstream_skipped(self, failed: SubTask, all_subs: dict):
        """ponytail: 失败的子任务 → 它所有依赖它的下游标 SKIPPED。"""
        for s in all_subs.values():
            if s.status == SubTaskStatus.PENDING and failed.name in s.deps:
                if SubTaskStatus.can_transition(s.status, SubTaskStatus.SKIPPED):
                    s.status = SubTaskStatus.SKIPPED
                    logger.info(f"子任务 {s.name} 因 {failed.name} 失败而 SKIPPED")

    async def _integrate_results(self, task: str, completed: dict) -> str:
        """ponytail: LLM 写整合要求 + Claude Code 整合。"""
        # 1. LLM 写整合摘要
        summary = "无完成子任务"
        if completed:
            try:
                if self.llm:
                    summary_resp = await self.llm.chat([{
                        "role": "user",
                        "content": (
                            f"基于以下 {len(completed)} 个子任务产物，写整合要求（不超过 200 字）：\n"
                            f"子任务：{list(completed.keys())}\n"
                            f"原始任务：{task[:300]}"
                        )
                    }], temperature=0.3, max_tokens=300)
                    summary = summary_resp[:500]
            except Exception as e:
                logger.warning(f"LLM 写整合摘要失败: {e}")

        # 2. 调 Claude Code 整合
        paths = list(completed.values())
        try:
            result = await self.claude_code.execute(
                task=f"整合要求：{summary}\n\n子产物路径：{paths}",
                context=f"原始任务：{task[:500]}",
                working_dir=self._client_dir or "/tmp",
            )
            if result.success:
                return result.output
        except Exception as e:
            logger.error(f"Claude Code 整合失败: {e}")

        # 3. 降级：直接拼所有子产物
        chunks = [f"## 子任务 {name}\n\n{open(p).read() if os.path.exists(p) else '(missing)'}\n" for name, p in completed.items()]
        return "\n\n".join(chunks)

    def _build_architecture_message(self, content: str, client_name: str, client_dir: str):
        return Message(
            content=content,
            cause_by="architecture_design",
            sent_from=self.agent_id,
            send_to={"*"},
            metadata={
                "action": "design_architecture",
                "client_name": client_name,
                "client_dir": client_dir,
                "v3": True,
            },
        )

    @property
    def _semaphore(self):
        """ponytail: 类级别信号量，最多 3 个子任务并行。"""
        if not hasattr(type(self), "_SEM"):
            type(self)._SEM = asyncio.Semaphore(3)
        return type(self)._SEM

    @property
    def _controller(self):
        """ponytail: LLM 总控（项目经理由 LLM 当）。"""
        if not hasattr(self, "_controller_inst"):
            self._controller_inst = _SubTaskController(self.llm)
        return self._controller_inst

    async def _design_architecture(self, last_message: Optional[Message]) -> Message:
        """
        设计技术架构

        1. 从 PRD 中提取客户名称
        2. 创建客户专属目录
        3. 调用 Claude Code 设计架构
        """
        prd_content = last_message.content if last_message else ""
        client_name = self._extract_client_name(prd_content)

        # 创建客户专属目录
        self._client_dir = self.claude_code.create_client_directory(client_name)
        logger.info(f"客户项目目录: {self._client_dir}")

        # 存储 PRD 内容
        self._context["prd"] = prd_content
        self._context["client_name"] = client_name

        task = """请根据客户需求设计技术架构。

要求：
1. 分析需求，确定技术选型
2. 设计系统架构和模块划分
3. 规划项目目录结构
4. 输出架构设计文档，保存为 architecture.md

注意：这是一个实际要交付的产品，请设计实用、可落地的方案。"""

        result = await self.claude_code.execute(
            task=task,
            context=f"客户需求：\n{prd_content}",
            working_dir=self._client_dir,
        )

        # ponytail: Claude Code CLI 静默失败时 sandbox 仍返 success=True，改用"文件存在"判定 + fallback
        if not result.success or not os.path.exists(os.path.join(self._client_dir, "architecture.md")):
            logger.warning("Claude Code CLI 未产出 architecture.md（success=%s），fallback 到 self.llm",
                          result.success)
            fallback = await self._llm_fallback_generate(self._client_dir, prd_content, task)
            if fallback:
                result = type("R", (), {"success": True, "output": fallback, "error": None})()

        if result.success:
            content = self._extract_output(result)
            self._context["architecture"] = content

            return Message(
                content=content,
                cause_by="architecture_design",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "design_architecture",
                    "client_name": client_name,
                    "client_dir": self._client_dir,
                },
            )
        else:
            return Message(
                content=f"架构设计失败：{result.error}",
                cause_by="error",
                sent_from=self.agent_id,
                metadata={"action": "design_architecture", "error": result.error},
            )

    async def _implement_feature(
        self, last_message: Optional[Message], messages: list[Message]
    ) -> Message:
        """
        实现功能代码

        调用 Claude Code 在客户目录下构建完整产品
        """
        prd_content = self._context.get("prd", "")
        architecture = self._context.get("architecture", "")
        client_dir = self._client_dir or self.claude_code.create_client_directory("default")

        task = """请根据架构设计，构建完整的产品。

要求：
1. 创建完整的项目结构
2. 实现所有核心功能
3. 编写配置文件（package.json、requirements.txt 等）
4. 确保项目可以直接运行
5. 添加 README.md 说明如何运行项目

这是一个要交付给客户的实际产品，请确保代码质量。"""

        context_parts = []
        if architecture:
            context_parts.append(f"架构设计：\n{architecture}")
        if prd_content:
            context_parts.append(f"客户需求：\n{prd_content}")

        result = await self.claude_code.execute(
            task=task,
            context="\n\n".join(context_parts) if context_parts else None,
            working_dir=client_dir,
        )

        if result.success:
            content = self._extract_output(result)
            self._context["implementation"] = content

            return Message(
                content=content,
                cause_by="implement_feature",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "implement_feature",
                    "client_dir": client_dir,
                },
            )
        else:
            return Message(
                content=f"代码实现失败：{result.error}",
                cause_by="error",
                sent_from=self.agent_id,
                metadata={"action": "implement_feature", "error": result.error},
            )

    async def _review_code(self, last_message: Optional[Message]) -> Message:
        """
        审查代码并生成摘要文档

        这是最终交付步骤，生成成果摘要文档给下游 Agent，
        同时生成 product.md 供客服 Agent 使用。
        """
        client_dir = self._client_dir or "/root"
        client_name = self._context.get("client_name", "unknown")
        prd_content = self._context.get("prd", "")

        # 让 Claude Code 审查代码并生成摘要
        task = """请完成以下工作：

1. 审查项目中的代码质量
2. 生成一份成果摘要文档（deliverable.md），包含：
   - 项目概述：实现了什么功能
   - 技术栈：使用了哪些技术
   - 项目结构：目录和文件说明
   - 如何运行：启动和部署说明
   - 注意事项：已知限制和建议

这是交付给其他团队成员的文档，请写得清晰、专业。"""

        result = await self.claude_code.execute(
            task=task,
            context=f"客户需求：\n{prd_content}" if prd_content else None,
            working_dir=client_dir,
        )

        # ponytail: Claude Code CLI 静默失败时 sandbox 也返回 success=True，改用"文件存在"判定
        if not result.success or not os.path.exists(os.path.join(client_dir, "deliverable.md")):
            logger.warning("Claude Code CLI 未产出文件（success=%s, err=%s），fallback 到 self.llm",
                          result.success, result.error)
            fallback = await self._llm_fallback_generate(client_dir, prd_content, task)
            if fallback:
                result = type("R", (), {"success": True, "output": fallback, "error": None})()

        if result.success:
            # 读取生成的摘要文档
            deliverable_path = os.path.join(client_dir, "deliverable.md")
            if os.path.exists(deliverable_path):
                with open(deliverable_path, "r", encoding="utf-8") as f:
                    deliverable_content = f.read()
            else:
                deliverable_content = self._extract_output(result)

            # 生成 product.md（供客服 Agent 使用）
            await self._generate_product_doc(client_dir, prd_content, deliverable_content)

            # 存储构建经验到 KnowledgeMemory
            await self._store_experience(client_name, prd_content, deliverable_content)

            # 生成完整的摘要
            summary = self._generate_summary(
                client_name=client_name,
                client_dir=client_dir,
                deliverable=deliverable_content,
            )

            return Message(
                content=summary,
                cause_by="review_code",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "review_code",
                    "client_name": client_name,
                    "client_dir": client_dir,
                    "deliverable_file": deliverable_path,
                },
            )
        else:
            return Message(
                content=f"代码审查失败：{result.error}",
                cause_by="error",
                sent_from=self.agent_id,
                metadata={"action": "review_code", "error": result.error},
            )

    async def _llm_fallback_generate(
        self, client_dir: str, prd_content: str, task: str
    ) -> Optional[str]:
        """
        ponytail: Claude Code CLI 不可用时的 fallback——直接调 LLM 生成产物。
        写 deliverable.md（成果摘要）+ index.html（最简官网），返回 deliverable 内容。
        """
        if not self.llm:
            logger.error("fallback 不可用：self.llm 为 None")
            return None
        try:
            os.makedirs(client_dir, exist_ok=True)
            # ponytail: deliverable（小）单次调，index_html（大）走 _smart_generate 自动分块
            deliverable = await self.llm.chat(
                [{"role": "user", "content": f"用 Markdown 写一份成果摘要（300 字内），覆盖：项目概述、技术栈、目录结构、运行方式。\n\n客户需求：\n{prd_content}"}],
                temperature=0.3, max_tokens=800,
            )
            index_html = await self._smart_generate(
                system="你是一个全栈工程师。生成完整 HTML5 单页官网源码。",
                user=(
                    f"===DELIVERABLE===\n（已独立生成，跳过）\n\n"
                    f"===INDEX_HTML===\n生成 HTML5 单页官网源码，含公司介绍、服务、关于、联系四个区块。\n"
                    f"深蓝/金色色调，响应式布局。\n\n客户需求：\n{prd_content}"
                ),
                max_tokens=8000, temperature=0.7,
            )
            # ponytail: 兼容 LLM 把 INDEX_HTML 内容也包了——剥掉前置 marker + fence
            import re
            if "===INDEX_HTML===" in index_html:
                index_html = index_html.split("===INDEX_HTML===", 1)[1]
            index_html = index_html.strip()
            index_html = re.sub(r"^```(?:html)?\s*\n", "", index_html)
            index_html = re.sub(r"\n```\s*$", "", index_html)
            index_html = re.sub(r"\*+", "", index_html)

            # 写盘
            with open(os.path.join(client_dir, "deliverable.md"), "w", encoding="utf-8") as f:
                f.write(deliverable)
            with open(os.path.join(client_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(index_html)

            logger.info(f"fallback 写入: {client_dir}/deliverable.md + index.html")
            return deliverable
        except Exception as e:
            logger.error(f"fallback 生成失败: {e}")
            return None

    async def _generate_product_doc(
        self, client_dir: str, prd_content: str, deliverable: str
    ) -> None:
        """
        生成产品知识文档 product.md

        供客服 Agent 读取，回答客户关于项目的使用和功能问题。
        """
        if not self.llm:
            logger.warning("LLM 不可用，跳过 product.md 生成")
            return

        system_prompt = """你是一个技术文档专家。根据项目构建结果，生成产品知识文档。

输出格式（Markdown）：
# {项目名称}

## 概述
一句话描述这个项目是什么、给谁用的

## 已实现功能
- 功能1：说明
- 功能2：说明
...

## 技术栈
- 使用的框架、语言、数据库等

## 使用说明
- 后台地址、登录方式
- 各功能模块的操作方法
- 写得具体到操作步骤，客户能直接照做

## 已知限制
- 当前版本的限制和注意事项

## 待实现（客户需求但未包含在当前版本）
- 功能A：简要说明
- 功能B：简要说明

注意：
- 只写已确认实现的功能，不要猜测
- "待实现"部分从需求中提取未完成的部分
- 语气面向客户，不要过于技术化"""

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"客户需求：\n{prd_content}\n\n构建结果摘要：\n{deliverable}",
            },
        ]

        try:
            doc_content = await self.llm.chat(messages, temperature=0.3, max_tokens=2000)

            # 写入客户项目目录
            doc_path = os.path.join(client_dir, "product.md")
            with open(doc_path, "w", encoding="utf-8") as f:
                f.write(doc_content)

            logger.info(f"产品文档已生成: {doc_path}")

        except Exception as e:
            logger.error(f"生成 product.md 失败: {e}")

    async def _store_experience(
        self, client_name: str, prd_content: str, deliverable: str
    ) -> None:
        """
        将构建经验存储到 KnowledgeMemory（Layer 3）

        存储内容包括：
        - 项目构建经验（用于未来类似需求参考）
        - 技术选型和架构决策
        """
        if not self.memory_manager:
            return

        # 用 LLM 提炼经验总结
        experience_text = deliverable
        if self.llm:
            try:
                system_prompt = """你是一个经验提炼专家。从项目构建结果中提炼可复用的经验，用 2-3 句话总结。

总结格式：
- 项目类型 + 技术栈
- 关键设计决策
- 可复用的经验"""
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"客户需求：{prd_content[:500]}\n\n构建结果：{deliverable[:1000]}"},
                ]
                experience_text = await self.llm.chat(messages, temperature=0.3, max_tokens=300)
            except Exception as e:
                logger.warning(f"LLM 提炼经验失败，使用原始文本: {e}")

        await self.remember(
            content=experience_text,
            memory_type="experience",
            metadata={
                "client_name": client_name,
                "project_type": self._infer_project_type(prd_content),
                "source": "developer_build",
            },
        )
        logger.info(f"构建经验已存储: client={client_name}")

    def _infer_project_type(self, prd_content: str) -> str:
        """从 PRD 推断项目类型"""
        prd_lower = prd_content.lower()
        type_keywords = {
            "website": ["网站", "官网", "web", "website", "网页", "主页"],
            "web_app": ["应用", "系统", "平台", "后台", "管理", "app", "application"],
            "ecommerce": ["电商", "商城", "购物", "店铺", "商品"],
            "api": ["api", "接口", "服务"],
            "mini_program": ["小程序", "微信"],
            "landing": ["落地页", "landing", "宣传"],
        }
        for ptype, keywords in type_keywords.items():
            if any(kw in prd_lower for kw in keywords):
                return ptype
        return "general"

    def _extract_client_name(self, prd_content: str) -> str:
        """从 PRD 中提取客户名称"""
        # 尝试从 PRD 中提取客户名称
        # 简单实现：使用时间戳作为客户标识
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 尝试从内容中提取公司名称
        import re
        patterns = [
            r"公司[：:]\s*(\S+)",
            r"客户[：:]\s*(\S+)",
            r"项目[：:]\s*(\S+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, prd_content)
            if match:
                return match.group(1)

        return f"client_{timestamp}"

    def _extract_output(self, result: ClaudeCodeResult) -> str:
        """从执行结果中提取输出内容"""
        if result.output:
            return result.output
        return "任务已执行完成，请查看生成的文件。"

    def _generate_summary(
        self,
        client_name: str,
        client_dir: str,
        deliverable: str,
    ) -> str:
        """生成完整的成果摘要"""
        return f"""# 开发成果摘要

## 客户信息
- 客户名称：{client_name}
- 项目目录：{client_dir}
- 完成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## 成果文档

{deliverable}

## 交付说明

项目代码已部署在服务器 `{client_dir}` 目录下。
其他团队成员可根据成果文档进行后续工作（部署、运维、财务核算等）。
"""

    def _extract_prd_content(self, messages: list[Message]) -> str:
        """从消息历史中提取 PRD 内容"""
        for msg in reversed(messages):
            if msg.cause_by == "create_prd":
                return msg.content
        return self._context.get("prd", "")


class _SubTaskController:
    """ponytail: LLM 当子任务的项目经理——决定 retry/skip/abort。"""

    def __init__(self, llm):
        self.llm = llm

    async def on_layer_start(self, layer: list, completed: dict) -> dict:
        """每层开始前 LLM 决定。"""
        if not self.llm:
            return {"action": "continue", "skip": []}
        try:
            resp = await self.llm.chat([{
                "role": "system", "content": "你是项目经理。输出严格 JSON，不加任何解释。",
            }, {
                "role": "user", "content": (
                    f"即将执行子任务层：{[s.name for s in layer]}\n"
                    f"已完成：{list(completed.keys())}\n"
                    "输出 JSON：{\"action\": \"continue|abort\", \"skip\": [\"name1\"]}"
                )
            }], temperature=0.3, max_tokens=150)
            return self._parse(resp, default={"action": "continue", "skip": []})
        except Exception as e:
            logger.warning(f"Controller.on_layer_start 失败: {e}")
            return {"action": "continue", "skip": []}

    async def on_subtask_fail(self, sub, error: str) -> dict:
        """子任务失败 LLM 决策。"""
        if not self.llm:
            return {"action": "skip"}
        try:
            resp = await self.llm.chat([{
                "role": "system", "content": "你是项目经理。输出严格 JSON。",
            }, {
                "role": "user", "content": (
                    f"子任务 `{sub.name}` 第 {sub.retries+1} 次失败：{error[:200]}\n"
                    "输出 JSON：{\"action\": \"retry|skip|abort\"}"
                )
            }], temperature=0.3, max_tokens=80)
            return self._parse(resp, default={"action": "skip"})
        except Exception as e:
            logger.warning(f"Controller.on_subtask_fail 失败: {e}")
            return {"action": "skip"}

    def _parse(self, raw: str, default: dict) -> dict:
        """容忍 markdown fence 解析 JSON。"""
        import re as _re
        try:
            s = raw.strip()
            s = _re.sub(r"^```(?:json)?\s*\n", "", s)
            s = _re.sub(r"\n```\s*$", "", s)
            return json.loads(s)
        except Exception:
            return default
