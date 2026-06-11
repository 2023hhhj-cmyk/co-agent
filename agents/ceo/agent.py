"""
CEO Agent - 首席执行官

职责：
1. 通过飞书与用户沟通，接收客户需求
2. 理解需求，必要时追问澄清
3. LLM 在项目模板约束内拆解任务
4. 将拆解方案发给用户确认
5. 确认后调度下级 Agent 执行（混合模式：简单同步、复杂异步）
6. 追踪进度，实时飞书通知

状态机：
IDLE → CLASSIFY → CHECK_COMPLETENESS → DECOMPOSE → WAIT_CONFIRM → EXECUTE → REPORT
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from agents.base import BaseAgent
from channels.feishu import FeishuClient
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message
from models.task import ProjectPlan, Task, TaskStatus
from tools.code.claude_code import ClaudeCodeTool
from tools.llm.client import LLMClient
from tools.llm.prompt import PromptManager

logger = logging.getLogger(__name__)

# CEO 角色定义
CEO_ROLE = AgentRole(
    name="ceo",
    profile="一人公司的CEO",
    goal="通过飞书与客户沟通，理解需求，拆解任务，调度团队执行，确保项目交付",
    constraints="在项目模板框架内拆解任务，复杂任务需用户确认后才执行",
    tools=["feishu", "task_decomposition", "team_coordination"],
    actions=[
        ActionDef(
            name="classify_intent",
            description="判断用户消息意图",
        ),
        ActionDef(
            name="check_completeness",
            description="检查需求信息是否完整",
        ),
        ActionDef(
            name="decompose_tasks",
            description="拆解任务",
        ),
        ActionDef(
            name="execute_plan",
            description="执行任务计划",
        ),
        ActionDef(
            name="report_progress",
            description="汇报进度",
        ),
    ],
    watch={"user_requirement", "task_complete", "report", "feishu_message"},
    react_mode=ReactMode.REACT,
    max_react_loop=3,
)


class CEOAgent(BaseAgent):
    """CEO Agent - 通过飞书与用户沟通，调度团队"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        message_bus: Any = None,
        feishu_client: Optional[FeishuClient] = None,
        team: Optional[dict[str, Any]] = None,
        base_dir: str = "/root",
    ):
        super().__init__(
            role=CEO_ROLE,
            llm_client=llm_client,
            message_bus=message_bus,
        )
        self.feishu = feishu_client
        self.team = team or {}  # role_name -> agent instance
        self.base_dir = base_dir

        # Claude Code 工具（用于浏览项目代码，优先级低于项目文件）
        self.claude_code = ClaudeCodeTool(
            base_dir=base_dir,
            timeout=30,
        )

        # 加载项目模板
        self.template = self._load_template()

        # 加载知识文件和人格文件
        self.product_doc = self._load_markdown("product.md")
        self.soul_doc = self._load_markdown("soul.md")

        # 活跃项目计划：chat_id -> ProjectPlan
        self._active_plans: dict[str, ProjectPlan] = {}

        # 消息去重
        self._processed_messages: set[str] = set()

    def _load_markdown(self, filename: str) -> str:
        """加载 Markdown 文件"""
        file_path = Path(__file__).parent / filename
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            logger.info(f"已加载 {filename}")
            return content
        logger.warning(f"{filename} 不存在: {file_path}")
        return ""

    def _load_template(self) -> dict:
        """加载项目模板"""
        template_path = Path(__file__).parent.parent.parent / "templates" / "project_template.yaml"
        if template_path.exists():
            with open(template_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        logger.warning(f"项目模板不存在: {template_path}")
        return {"phases": [], "agent_capabilities": {}, "decomposition_rules": []}

    # ========== 飞书消息处理入口 ==========

    async def handle_feishu_message(
        self,
        chat_id: str,
        text: str,
        message_id: str,
        sender_id: str = "",
        chat_type: str = "p2p",
    ) -> None:
        """
        处理来自飞书的消息（主入口）

        Args:
            chat_id: 飞书会话 ID
            text: 消息文本
            message_id: 消息 ID（用于去重和回复）
            sender_id: 发送者 ID（open_id）
            chat_type: 会话类型（p2p=私聊, group=群聊）
        """
        # 幂等检查
        if message_id in self._processed_messages:
            logger.info(f"重复消息，跳过: {message_id}")
            return
        self._processed_messages.add(message_id)
        # 限制去重集合大小
        if len(self._processed_messages) > 10000:
            self._processed_messages = set(list(self._processed_messages)[-5000:])

        # 记录会话上下文（用于后续发送消息）
        self._current_chat_id = chat_id
        self._current_sender_id = sender_id
        self._current_chat_type = chat_type

        logger.info(f"CEO 收到飞书消息: chat_id={chat_id}, text={text[:50]}...")

        # 立刻回复"收到"，让用户知道消息已收到
        await self._send_feishu_text(chat_id, "收到，正在处理...")

        # 检查是否是卡片回调（确认/修改）
        if text.startswith("{"):
            try:
                callback_data = json.loads(text)
                action = callback_data.get("action")
                plan_id = callback_data.get("plan_id")
                if action and plan_id:
                    await self._handle_card_callback(chat_id, action, plan_id, message_id)
                    return
            except json.JSONDecodeError:
                pass

        # 分类意图
        intent = await self._classify_intent(text, chat_id)
        logger.info(f"意图分类: {intent}")

        if intent == "new_requirement":
            await self._handle_new_requirement(chat_id, text, message_id)
        elif intent == "check_progress":
            await self._handle_check_progress(chat_id)
        elif intent == "modify":
            await self._handle_modify(chat_id, text)
        elif intent == "general_question":
            await self._handle_general_question(chat_id, text)
        else:
            await self._send_feishu_text(chat_id, "收到消息，但不太确定您想做什么。您可以：\n1. 发送客户需求，我来拆解执行\n2. 发送「进度」查看当前项目状态")

    # ========== 意图分类 ==========

    async def _classify_intent(self, text: str, chat_id: str) -> str:
        """LLM 判断用户意图"""
        # 快捷关键词匹配
        progress_keywords = ["进度", "状态", "进展", "怎么样", "完成了吗", "做到哪"]
        if any(kw in text for kw in progress_keywords):
            if chat_id in self._active_plans:
                return "check_progress"

        modify_keywords = ["修改", "调整", "改一下", "变更", "重新"]
        if any(kw in text for kw in modify_keywords):
            if chat_id in self._active_plans:
                return "modify"

        # LLM 分类
        if not self.llm:
            return "new_requirement"

        system_prompt = """你是一个意图分类器。根据用户消息，判断意图类别。

可选意图：
- new_requirement: 用户提出了新的需求或任务
- check_progress: 用户想查看项目进度
- modify: 用户想修改已有的计划
- general_question: 用户在问一般性问题

只输出意图类别，不要输出其他内容。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"用户消息：{text}"},
        ]

        try:
            response = await self.llm.chat(messages, temperature=0.1, max_tokens=50)
            response = response.strip().lower()
            for intent in ["new_requirement", "check_progress", "modify", "general_question"]:
                if intent in response:
                    return intent
        except Exception as e:
            logger.error(f"意图分类失败: {e}")

        return "new_requirement"

    # ========== 需求处理 ==========

    async def _handle_new_requirement(
        self, chat_id: str, text: str, message_id: str
    ) -> None:
        """处理新需求：检查完整性 → 拆解 → 确认"""
        # 检查需求完整性
        is_complete, feedback = await self._check_completeness(text)

        if not is_complete:
            # 需求不完整，追问
            await self._send_feishu_text(chat_id, feedback)
            return

        # 需求完整，拆解任务
        await self._send_feishu_text(chat_id, "收到！正在为您拆解任务，请稍候...")

        plan = await self._decompose_tasks(text, chat_id, message_id)
        if not plan:
            await self._send_feishu_text(chat_id, "抱歉，任务拆解出现问题，请稍后重试。")
            return

        # 保存计划
        self._active_plans[chat_id] = plan

        # 发送确认卡片
        card = plan.to_confirm_card()
        await self.feishu.send_card(chat_id, card)

    async def _check_completeness(self, requirement: str) -> tuple[bool, str]:
        """
        检查需求信息是否完整

        Returns:
            (是否完整, 反馈信息)
        """
        if not self.llm:
            return True, ""

        system_prompt = """你是一个需求完整性检查器。判断用户的需求描述是否足够清晰，能让团队开始工作。

检查要点：
1. 要做什么？（功能/产品类型）
2. 给谁用？（目标用户）
3. 有什么核心功能？（至少一个）
4. 有没有特殊要求？（技术栈、设计风格等 — 可选）

如果信息足够，输出：COMPLETE
如果信息不够，输出具体需要追问的问题（一个问题即可，简洁明了）。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"用户需求：{requirement}"},
        ]

        try:
            response = await self.llm.chat(messages, temperature=0.3, max_tokens=200)
            response = response.strip()

            if "COMPLETE" in response.upper():
                return True, ""
            else:
                return False, response
        except Exception as e:
            logger.error(f"需求完整性检查失败: {e}")
            return True, ""  # 出错时默认完整，不阻塞流程

    # ========== 任务拆解 ==========

    async def _decompose_tasks(
        self, requirement: str, chat_id: str, message_id: str
    ) -> Optional[ProjectPlan]:
        """LLM 在模板约束内拆解任务"""
        if not self.llm:
            return self._default_plan(requirement, chat_id, message_id)

        # 构建模板上下文
        template_context = self._build_template_context()

        system_prompt = f"""你是一个项目CEO。根据客户需求，拆解为可执行的子任务。你直接面对客户需求，没有产品经理，需要把模糊需求转化为 Developer 能直接执行的详细描述。

## 项目模板
{template_context}

## 拆解规则
- 每个阶段至少有一个任务
- 必须覆盖所有 required_agents
- optional_agents 根据需求决定是否参与
- 通过 dependencies 定义任务依赖关系
- 下一阶段的任务依赖上一阶段的任务完成

## 给 Developer 的任务描述要求（最重要！）
Developer 使用 Claude Code 构建产品，它需要清晰的输入。给 Developer 的 description 必须包含：
1. 要做什么：一句话概括产品
2. 目标用户：给谁用的
3. 功能清单：列出所有需要实现的功能点
4. 页面/模块结构：有哪些页面或模块，各自的作用
5. 技术要求：技术栈偏好、部署方式等（如有）
6. 参考：客户提到的参考样式或竞品（如有）

## 输出格式
严格输出 JSON，不要包含其他文本：
{{
  "tasks": [
    {{
      "title": "任务标题",
      "description": "具体要做什么",
      "assigned_to": "agent_role_name",
      "dependencies": [],
      "priority": 2
    }}
  ]
}}

assigned_to 可选值：developer, frontend_expert, devops, finance"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"客户需求：{requirement}"},
        ]

        try:
            response = await self.llm.chat(messages, temperature=0.3, max_tokens=2000)
            tasks_data = self._parse_tasks_json(response)

            if not tasks_data:
                logger.warning("LLM 返回的任务数据解析失败，使用默认方案")
                return self._default_plan(requirement, chat_id, message_id)

            tasks = []
            for td in tasks_data:
                task = Task(
                    title=td.get("title", "未命名任务"),
                    description=td.get("description", ""),
                    assigned_to=td.get("assigned_to", "developer"),
                    dependencies=td.get("dependencies", []),
                    priority=td.get("priority", 2),
                )
                tasks.append(task)

            plan = ProjectPlan(
                requirement=requirement,
                tasks=tasks,
                chat_id=chat_id,
                user_message_id=message_id,
            )
            return plan

        except Exception as e:
            logger.error(f"任务拆解失败: {e}")
            return self._default_plan(requirement, chat_id, message_id)

    def _build_template_context(self) -> str:
        """构建模板上下文文本"""
        lines = []

        for phase in self.template.get("phases", []):
            lines.append(f"### {phase['name']}")
            lines.append(f"- 必须参与: {', '.join(phase.get('required_agents', []))}")
            if phase.get("optional_agents"):
                lines.append(f"- 可选参与: {', '.join(phase['optional_agents'])}")
            lines.append(f"- 说明: {phase.get('description', '')}")
            lines.append(f"- 可并行: {'是' if phase.get('can_parallel') else '否'}")
            lines.append("")

        lines.append("### Agent 能力")
        for role, info in self.template.get("agent_capabilities", {}).items():
            lines.append(f"- **{info.get('name', role)}** ({role}): {info.get('capabilities', '')}")

        return "\n".join(lines)

    def _parse_tasks_json(self, response: str) -> Optional[list[dict]]:
        """解析 LLM 返回的任务 JSON"""
        text = response.strip()
        # 去掉 markdown 代码块
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            data = json.loads(text)
            if isinstance(data, dict) and "tasks" in data:
                return data["tasks"]
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            logger.warning(f"JSON 解析失败: {text[:200]}")
        return None

    def _default_plan(
        self, requirement: str, chat_id: str, message_id: str
    ) -> ProjectPlan:
        """生成默认任务方案（LLM 不可用时的回退）"""
        tasks = [
            Task(title="设计与开发", description=requirement, assigned_to="developer"),
            Task(title="前端页面", description="设计并实现高质量前端页面", assigned_to="frontend_expert"),
            Task(title="部署上线", description="部署到生产环境", assigned_to="devops", dependencies=[]),
            Task(title="财务结算", description="记录成本，生成账单", assigned_to="finance", dependencies=[]),
        ]

        # 设置依赖关系
        tasks[2].dependencies = [tasks[0].id]  # 部署依赖开发
        tasks[3].dependencies = [tasks[2].id]  # 财务依赖部署

        return ProjectPlan(
            requirement=requirement,
            tasks=tasks,
            chat_id=chat_id,
            user_message_id=message_id,
        )

    # ========== 卡片回调处理 ==========

    async def _handle_card_callback(
        self, chat_id: str, action: str, plan_id: str, message_id: str
    ) -> None:
        """处理飞书卡片按钮回调"""
        plan = self._active_plans.get(chat_id)
        if not plan or plan.id != plan_id:
            await self._send_feishu_text(chat_id, "未找到对应的项目计划，请重新提交需求。")
            return

        if action == "confirm":
            await self._execute_plan(chat_id, plan)
        elif action == "modify":
            await self._send_feishu_text(chat_id, "请告诉我需要修改哪些部分，或直接发送修改后的需求。")
            plan.status = "planning"
        else:
            await self._send_feishu_text(chat_id, f"未知操作: {action}")

    # ========== 任务执行 ==========

    async def _execute_plan(self, chat_id: str, plan: ProjectPlan) -> None:
        """按依赖关系执行任务计划"""
        plan.status = "executing"
        await self._send_feishu_text(chat_id, "✅ 已确认！开始执行任务...")

        # 按依赖层级执行
        while not plan.is_all_done():
            ready_tasks = plan.get_ready_tasks()
            if not ready_tasks:
                # 没有就绪任务但也没全部完成 → 可能有失败导致的死锁
                running = plan.get_running_tasks()
                if not running:
                    logger.error("任务执行死锁：无就绪任务也无运行中任务")
                    await self._send_feishu_text(chat_id, "⚠️ 任务执行出现问题，请检查。")
                    break
                # 等待运行中的任务
                await asyncio.sleep(2)
                continue

            # 标记为运行中
            for task in ready_tasks:
                task.mark_running()

            # 通知用户开始执行
            task_names = ", ".join(t.title for t in ready_tasks)
            await self._send_feishu_text(chat_id, f"🔄 正在执行: {task_names}")

            # 并行执行就绪任务
            execution_tasks = [
                self._execute_single_task(chat_id, plan, task)
                for task in ready_tasks
            ]
            await asyncio.gather(*execution_tasks, return_exceptions=True)

        # 全部完成，汇总报告
        plan.status = "completed"
        plan.completed_at = datetime.now()
        await self._send_completion_report(chat_id, plan)

    async def _execute_single_task(
        self, chat_id: str, plan: ProjectPlan, task: Task
    ) -> None:
        """执行单个任务"""
        agent = self.team.get(task.assigned_to)
        if not agent:
            task.mark_failed(f"Agent {task.assigned_to} 不可用")
            await self._send_feishu_text(chat_id, f"❌ 任务 [{task.title}] 失败: Agent {task.assigned_to} 不可用")
            return

        logger.info(f"执行任务: {task.title} -> {task.assigned_to}")

        try:
            # 构造输入消息
            # 收集依赖任务的结果作为上下文
            context_parts = [f"任务: {task.title}\n要求: {task.description}"]
            for dep_id in task.dependencies:
                dep_task = plan.get_task(dep_id)
                if dep_task and dep_task.result:
                    context_parts.append(f"前置任务 [{dep_task.title}] 的结果:\n{dep_task.result}")

            input_message = Message(
                content="\n\n---\n\n".join(context_parts),
                cause_by="ceo_task_assignment",
                sent_from=self.agent_id,
                send_to={task.assigned_to},
                metadata={
                    "plan_id": plan.id,
                    "task_id": task.id,
                    "requirement": plan.requirement,
                },
            )

            # 混合模式执行
            agent_info = self.template.get("agent_capabilities", {}).get(task.assigned_to, {})
            is_sync = agent_info.get("sync", True)

            if is_sync:
                # 同步执行（简单 Agent）
                result = await asyncio.wait_for(
                    agent.run(input_message),
                    timeout=300,  # 5 分钟超时
                )
            else:
                # 异步执行（复杂 Agent，如 Developer）
                result = await asyncio.wait_for(
                    agent.run(input_message),
                    timeout=900,  # 15 分钟超时
                )

            # 记录结果
            result_text = result.content if result else "任务已完成"
            task.mark_completed(result_text[:500])
            await self._send_feishu_text(chat_id, f"✅ 任务 [{task.title}] 已完成")

        except asyncio.TimeoutError:
            task.mark_failed("执行超时")
            await self._send_feishu_text(chat_id, f"⏰ 任务 [{task.title}] 执行超时")
        except Exception as e:
            task.mark_failed(str(e))
            logger.error(f"任务执行失败: {task.title} - {e}")
            await self._send_feishu_text(chat_id, f"❌ 任务 [{task.title}] 失败: {str(e)[:100]}")

    # ========== 进度查询 ==========

    async def _handle_check_progress(self, chat_id: str) -> None:
        """处理进度查询"""
        plan = self._active_plans.get(chat_id)
        if not plan:
            await self._send_feishu_text(chat_id, "当前没有进行中的项目。请发送客户需求开始。")
            return

        progress_text = plan.get_progress_text()
        await self._send_feishu_text(chat_id, progress_text)

    # ========== 修改处理 ==========

    async def _handle_modify(self, chat_id: str, text: str) -> None:
        """处理修改请求"""
        plan = self._active_plans.get(chat_id)
        if not plan:
            await self._send_feishu_text(chat_id, "当前没有进行中的项目。请发送新需求。")
            return

        # 如果正在执行中，不允许修改
        if plan.status == "executing":
            await self._send_feishu_text(chat_id, "项目正在执行中，无法修改。请等待完成后再发起新项目。")
            return

        # 重新拆解
        await self._send_feishu_text(chat_id, "好的，正在根据您的反馈重新拆解任务...")
        new_plan = await self._decompose_tasks(
            f"原始需求：{plan.requirement}\n\n用户反馈：{text}",
            chat_id,
            plan.user_message_id,
        )
        if new_plan:
            self._active_plans[chat_id] = new_plan
            card = new_plan.to_confirm_card()
            await self.feishu.send_card(chat_id, card)
        else:
            await self._send_feishu_text(chat_id, "重新拆解失败，请稍后重试。")

    # ========== 通用问答 ==========

    async def _handle_general_question(self, chat_id: str, text: str) -> None:
        """处理一般性问题"""
        response = await self._get_general_answer(text)
        await self._send_feishu_text(chat_id, response)

    async def _get_general_answer(self, text: str) -> str:
        """获取一般性问题的答案（返回字符串，不发送消息）"""
        if not self.llm:
            return "我是 Muzhu-AI 的 CEO Agent，可以帮您拆解需求并调度团队执行。请发送客户需求开始。"

        # 读取所有项目的 claude.md 和 product.md
        project_context = await self._read_all_project_files()

        # 构建系统提示词，包含人格和知识
        system_parts = ["你是 Muzhu-AI 多Agent协同系统的 CEO，老板的专属助手。"]

        if self.soul_doc:
            system_parts.append(f"\n## 你的人格定义\n{self.soul_doc}")

        if self.product_doc:
            system_parts.append(f"\n## 公司和产品信息\n{self.product_doc}")

        if project_context:
            system_parts.append(f"\n## 所有项目信息\n{project_context}")

        system_parts.append("""
## 回答要求
- 根据以上信息回答老板的问题
- 如果涉及具体项目，使用该项目的 claude.md（项目概述）和 product.md（进度）回答
- 如果问题超出你的能力范围，诚实说需要确认
- 回答简洁明了，不要过于冗长""")

        system_prompt = "\n".join(system_parts)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        try:
            response = await self.llm.chat(messages, temperature=0.5, max_tokens=500)
            return response
        except Exception:
            return "我是 Muzhu-AI 的 CEO Agent，可以帮您拆解需求并调度团队执行。请发送客户需求开始。"

    async def _read_all_project_files(self) -> str:
        """读取所有项目的 claude.md 和 product.md 文件"""
        import os

        project_files = []

        try:
            # 遍历 /root/ 下的所有目录
            for item in os.listdir(self.base_dir):
                item_path = os.path.join(self.base_dir, item)
                if not os.path.isdir(item_path):
                    continue

                # 跳过隐藏目录和系统目录
                if item.startswith(".") or item in ["venv", "__pycache__", "node_modules"]:
                    continue

                project_info = []

                # 读取 claude.md（项目概述）
                claude_md_path = os.path.join(item_path, "claude.md")
                if os.path.exists(claude_md_path):
                    try:
                        with open(claude_md_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        project_info.append(f"### {item} - 项目概述\n{content[:500]}")
                    except Exception as e:
                        logger.warning(f"读取 {claude_md_path} 失败: {e}")

                # 读取 product.md（项目进度）
                product_md_path = os.path.join(item_path, "product.md")
                if os.path.exists(product_md_path):
                    try:
                        with open(product_md_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        project_info.append(f"### {item} - 项目进度\n{content[:500]}")
                    except Exception as e:
                        logger.warning(f"读取 {product_md_path} 失败: {e}")

                if project_info:
                    project_files.append("\n\n".join(project_info))

            if project_files:
                return "\n\n---\n\n".join(project_files)
            else:
                return ""

        except Exception as e:
            logger.error(f"读取项目文件失败: {e}")
            return ""

    # ========== 完成报告 ==========

    async def _send_completion_report(self, chat_id: str, plan: ProjectPlan) -> None:
        """发送项目完成报告"""
        total = len(plan.tasks)
        completed = sum(1 for t in plan.tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in plan.tasks if t.status == TaskStatus.FAILED)

        report_lines = [
            f"🎉 项目完成！",
            f"",
            f"需求: {plan.requirement[:100]}",
            f"",
            f"📊 执行结果:",
        ]

        for task in plan.tasks:
            report_lines.append(task.to_summary())

        report_lines.extend([
            f"",
            f"总计: {total} 个任务, {completed} 完成, {failed} 失败",
        ])

        # 汇总各 Agent 的产出
        report_lines.append(f"\n📝 产出摘要:")
        for task in plan.tasks:
            if task.status == TaskStatus.COMPLETED and task.result:
                report_lines.append(f"\n**{task.title}** ({task.assigned_to}):")
                report_lines.append(task.result[:200])

        await self._send_feishu_text(chat_id, "\n".join(report_lines))

    # ========== 工具方法 ==========

    async def _send_feishu_text(self, chat_id: str, text: str) -> None:
        """通过飞书发送文本消息（自动判断私聊/群聊）"""
        if not self.feishu:
            logger.warning(f"飞书客户端未初始化，消息未发送: {text[:50]}")
            return
        try:
            # 私聊用 open_id 发送，群聊用 chat_id
            chat_type = getattr(self, "_current_chat_type", "p2p")
            if chat_type == "p2p" and self._current_sender_id:
                await self.feishu.send_to_user(self._current_sender_id, text)
            else:
                await self.feishu.send_text(chat_id, text)
        except Exception as e:
            logger.error(f"飞书发送失败: {e}")

    # ========== BaseAgent 接口实现 ==========

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """
        思考阶段

        CEO 的主要逻辑已迁移到 handle_feishu_message，
        此方法处理来自内部 MessageBus 的消息（如 Agent 完成通知）。
        """
        if not messages:
            return None

        last_message = messages[-1]

        if last_message.cause_by in ("task_complete", "report"):
            return self.role.get_action_by_name("report_progress")

        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """
        执行阶段

        处理内部消息（Agent 完成通知等）。
        """
        messages = context.get("messages", []) if context else []
        last_message = messages[-1] if messages else None

        if action.name == "report_progress":
            content = last_message.content if last_message else "无报告内容"
            return Message(
                content=f"团队报告: {content}",
                cause_by="report",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={"action": "report_progress"},
            )

        return Message(
            content=f"CEO 已处理: {action.name}",
            cause_by="ceo_action",
            sent_from=self.agent_id,
        )
