"""
前端专家 Agent

职责：
1. 分析前端需求，确定页面类型和风格
2. 初始化 Next.js + Tailwind CSS + shadcn/ui 项目
3. 生成高质量、可生产的前端页面
4. 自动审查和打磨设计质量

工作流程：
- 收到前端任务 → 分析需求 → 初始化项目 → 实现页面 → 审查打磨

技术栈：Next.js + Tailwind CSS + shadcn/ui
设计规范：通过 .claude/skills/ 注入（frontend-design、brand-guidelines、
         web-design-guidelines、react-best-practices、playwright）
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agents.base import BaseAgent
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message
from tools.code.claude_code import ClaudeCodeTool, ClaudeCodeResult
from tools.llm.client import LLMClient

logger = logging.getLogger(__name__)

# 前端专家角色定义
FRONTEND_EXPERT_ROLE = AgentRole(
    name="frontend_expert",
    profile="前端专家，专注于高质量 Web 前端设计与开发",
    goal="根据需求生成美观、响应式、可生产的前端页面，使用 Next.js + Tailwind CSS + shadcn/ui 技术栈",
    constraints=(
        "使用 Next.js + Tailwind CSS + shadcn/ui 技术栈；"
        "遵循设计最佳实践，避免通用 AI 模板感；"
        "确保页面响应式、可访问、性能良好"
    ),
    tools=["claude_code", "playwright", "design_templates"],
    actions=[
        ActionDef(
            name="analyze_requirement",
            description="分析前端需求，确定页面类型、风格和组件需求",
            prompt_template="请分析以下前端需求：\n{requirement}",
        ),
        ActionDef(
            name="setup_project",
            description="初始化 Next.js + Tailwind CSS + shadcn/ui 项目",
            prompt_template="请根据以下分析初始化前端项目：\n{analysis}",
        ),
        ActionDef(
            name="implement_frontend",
            description="实现前端页面代码",
            prompt_template="请根据以下需求和分析实现前端页面：\n{requirement}\n\n分析：\n{analysis}",
        ),
        ActionDef(
            name="review_and_polish",
            description="审查设计质量并打磨细节",
            prompt_template="请审查以下前端页面的设计质量：\n{implementation}",
        ),
    ],
    watch={"ceo_task_assignment", "frontend_task", "ui_design", "create_prd"},
    react_mode=ReactMode.REACT,
    max_react_loop=5,
)


class FrontendExpertAgent(BaseAgent):
    """前端专家 Agent"""

    # 设计规范 Skills 目录
    SKILLS_DIR = Path(__file__).parent.parent.parent / ".claude" / "skills"

    # 需要注入的 Skills（按优先级排序）
    DESIGN_SKILLS = [
        "frontend-design",
        "brand-guidelines",
        "web-design-guidelines",
        "react-best-practices",
    ]

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        message_bus: Any = None,
        base_dir: str = "/root",
    ):
        super().__init__(
            role=FRONTEND_EXPERT_ROLE,
            llm_client=llm_client,
            message_bus=message_bus,
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
        # 缓存 Skills 内容
        self._skills_context: Optional[str] = None

    # ========== Skills 注入 ==========

    def _load_skills_context(self) -> str:
        """
        加载 .claude/skills/ 下的 SKILL.md 内容作为设计规范 context

        这是核心设计：将已安装的 Skills 指令注入到 Claude Code 调用中，
        让每次代码生成都自带设计规范知识。
        """
        if self._skills_context is not None:
            return self._skills_context

        parts = []
        for skill_name in self.DESIGN_SKILLS:
            skill_md = self.SKILLS_DIR / skill_name / "SKILL.md"
            if skill_md.exists():
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    # 去掉 YAML frontmatter，只保留正文
                    if content.startswith("---"):
                        parts_idx = content.find("---", 3)
                        if parts_idx != -1:
                            content = content[parts_idx + 3:].strip()
                    parts.append(f"## {skill_name}\n\n{content}")
                    logger.info(f"已加载设计规范: {skill_name}")
                except Exception as e:
                    logger.warning(f"加载 Skill {skill_name} 失败: {e}")
            else:
                logger.warning(f"Skill 文件不存在: {skill_md}")

        self._skills_context = "\n\n---\n\n".join(parts) if parts else ""
        return self._skills_context

    # ========== Think-Act 循环 ==========

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """
        思考阶段：根据消息决定下一步动作

        流程：analyze_requirement → setup_project → implement_frontend → review_and_polish
        """
        if not messages:
            return None

        last_message = messages[-1]

        # 收到新任务 → 分析需求
        if last_message.cause_by in ("ceo_task_assignment", "frontend_task", "ui_design", "create_prd"):
            return self.role.get_action_by_name("analyze_requirement")

        # 分析完成 → 初始化项目
        if last_message.cause_by == "frontend_analysis":
            return self.role.get_action_by_name("setup_project")

        # 项目初始化完成 → 实现页面
        if last_message.cause_by == "frontend_setup":
            return self.role.get_action_by_name("implement_frontend")

        # 实现完成 → 审查打磨
        if last_message.cause_by == "frontend_implement":
            return self.role.get_action_by_name("review_and_polish")

        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """
        执行阶段：调用 Claude Code 执行动作
        """
        messages = context.get("messages", []) if context else []
        last_message = messages[-1] if messages else None

        if action.name == "analyze_requirement":
            return await self._analyze_requirement(last_message)
        elif action.name == "setup_project":
            return await self._setup_project(last_message)
        elif action.name == "implement_frontend":
            return await self._implement_frontend(last_message, messages)
        elif action.name == "review_and_polish":
            return await self._review_and_polish(last_message)
        else:
            return Message(
                content=f"未知动作：{action.name}",
                cause_by="error",
                sent_from=self.agent_id,
            )

    # ========== 动作实现 ==========

    async def _analyze_requirement(self, last_message: Optional[Message]) -> Message:
        """
        分析前端需求

        用 LLM 分析需求，确定：
        - 页面类型（落地页/仪表盘/表单页/展示页等）
        - 设计风格（极简/现代/商务/活泼等）
        - 需要的组件和区块
        - 响应式策略
        """
        requirement = last_message.content if last_message else ""
        client_name = self._extract_client_name(requirement)

        # 创建客户专属目录
        self._client_dir = self.claude_code.create_client_directory(client_name)
        logger.info(f"前端项目目录: {self._client_dir}")

        # 存储上下文
        self._context["requirement"] = requirement
        self._context["client_name"] = client_name

        if not self.llm:
            # LLM 不可用时的默认分析
            analysis = f"""# 前端需求分析

## 页面类型
- 通用 Web 页面

## 设计风格
- 现代、简洁

## 需要的组件
- 导航栏
- 主内容区
- 页脚

## 技术栈
- Next.js + Tailwind CSS + shadcn/ui

## 需求原文
{requirement}
"""
        else:
            system_prompt = """你是一个前端需求分析专家。根据客户需求，分析并输出结构化的前端需求分析报告。

分析要点：
1. 页面类型：落地页(Landing Page)、管理后台(Dashboard)、表单页(Form)、产品展示页、文档站、电商页等
2. 设计风格：极简(Minimal)、现代(Modern)、商务(Corporate)、活泼(Playful)、科技感(Tech)等
3. 核心区块：首屏 Hero、功能特性、定价表、用户评价、CTA、导航栏、页脚等
4. 需要的 shadcn/ui 组件：button、card、input、form、dialog、sheet、tabs、badge、table 等
5. 响应式策略：移动端优先还是桌面端优先
6. 特殊要求：动画、暗色模式、多语言等

输出格式：清晰的 Markdown 文档。"""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"客户需求：\n{requirement}"},
            ]

            try:
                analysis = await self.llm.chat(messages, temperature=0.3, max_tokens=1500)
            except Exception as e:
                logger.error(f"需求分析失败: {e}")
                analysis = f"需求分析失败，将直接根据原始需求实现。\n\n原始需求：{requirement}"

        self._context["analysis"] = analysis

        return Message(
            content=analysis,
            cause_by="frontend_analysis",
            sent_from=self.agent_id,
            send_to={"*"},
            metadata={
                "action": "analyze_requirement",
                "client_name": client_name,
                "client_dir": self._client_dir,
            },
        )

    async def _setup_project(self, last_message: Optional[Message]) -> Message:
        """
        初始化前端项目

        调用 Claude Code CLI，在客户目录下创建 Next.js + Tailwind CSS + shadcn/ui 项目。
        """
        analysis = self._context.get("analysis", "")
        client_dir = self._client_dir or self.claude_code.create_client_directory("default")

        # 构建任务描述
        task = """请完成以下前端项目初始化工作：

1. 使用 create-next-app 初始化 Next.js 项目（App Router、TypeScript、Tailwind CSS、ESLint）
2. 初始化 shadcn/ui（选择 New York 风格、Zinc 色系）
3. 根据需求安装必要的 shadcn/ui 组件（button、card、input、form、dialog、sheet、tabs、badge、table 等）
4. 创建基础的项目结构（app/、components/、lib/）
5. 配置 tailwind.config.js 的主题（颜色、字体、圆角、阴影）
6. 创建一个简单的布局组件（Layout）包含导航栏和页脚

请确保：
- 使用 TypeScript
- 使用 App Router（不是 Pages Router）
- shadcn/ui 组件安装在 components/ui/ 目录
- 自定义组件放在 components/ 目录
- 全局样式在 app/globals.css"""

        # 注入 Skills 设计规范
        skills_context = self._load_skills_context()
        context_parts = []
        if skills_context:
            context_parts.append(f"## 设计规范（必须遵循）\n\n{skills_context}")
        if analysis:
            context_parts.append(f"## 需求分析\n\n{analysis}")

        result = await self.claude_code.execute(
            task=task,
            context="\n\n".join(context_parts) if context_parts else None,
            working_dir=client_dir,
        )

        # Claude Code CLI 可能因 warning 返回非零退出码，检查项目是否实际创建成功
        project_json = os.path.join(client_dir, "package.json")
        if result.success or os.path.exists(project_json):
            content = self._extract_output(result)
            self._context["setup"] = content

            return Message(
                content=content,
                cause_by="frontend_setup",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "setup_project",
                    "client_dir": client_dir,
                },
            )
        else:
            return Message(
                content=f"项目初始化失败：{result.error}",
                cause_by="error",
                sent_from=self.agent_id,
                metadata={"action": "setup_project", "error": result.error},
            )

    async def _implement_frontend(
        self, last_message: Optional[Message], messages: list[Message]
    ) -> Message:
        """
        实现前端页面

        调用 Claude Code CLI，根据需求分析生成具体的页面代码。
        注入 Skills 设计规范确保设计质量。
        """
        requirement = self._context.get("requirement", "")
        analysis = self._context.get("analysis", "")
        setup = self._context.get("setup", "")
        client_dir = self._client_dir or self.claude_code.create_client_directory("default")

        task = """请根据需求分析，实现完整的前端页面。

要求：
1. 实现所有需要的页面和组件
2. 使用 shadcn/ui 组件库，不要自己造轮子
3. 确保响应式设计（移动端和桌面端都好看）
4. 注意视觉层次：字号、间距、颜色对比
5. 添加适当的 hover/focus 交互效果
6. 使用 Lucide Icons 图标
7. 确保页面可以直接运行（npm run dev）

代码风格要求：
- 组件拆分清晰，不要把所有代码写在一个文件里
- 使用 TypeScript 类型定义
- 使用 Tailwind CSS 的工具类，不要写自定义 CSS
- 颜色使用 shadcn/ui 的 CSS 变量（--primary、--secondary 等）"""

        # 注入 Skills 设计规范
        skills_context = self._load_skills_context()
        context_parts = []
        if skills_context:
            context_parts.append(f"## 设计规范（必须遵循）\n\n{skills_context}")
        if analysis:
            context_parts.append(f"## 需求分析\n\n{analysis}")
        if requirement:
            context_parts.append(f"## 原始需求\n\n{requirement}")

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
                cause_by="frontend_implement",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={
                    "action": "implement_frontend",
                    "client_dir": client_dir,
                },
            )
        else:
            return Message(
                content=f"页面实现失败：{result.error}",
                cause_by="error",
                sent_from=self.agent_id,
                metadata={"action": "implement_frontend", "error": result.error},
            )

    async def _review_and_polish(self, last_message: Optional[Message]) -> Message:
        """
        审查设计质量并打磨

        1. 运行项目，检查是否有编译错误
        2. 使用 Playwright 截图预览
        3. 用 LLM 审查设计质量
        4. 必要时迭代修改
        """
        requirement = self._context.get("requirement", "")
        analysis = self._context.get("analysis", "")
        client_dir = self._client_dir or "/root"
        client_name = self._context.get("client_name", "unknown")

        task = """请完成以下审查和打磨工作：

1. 运行 `npm run dev` 确认项目没有编译错误
2. 检查所有页面是否正常渲染
3. 审查以下设计质量维度：
   - 视觉层次：字号、颜色、间距是否合理
   - 响应式：移动端和桌面端是否都好看
   - 一致性：颜色、字体、间距是否统一
   - 可访问性：对比度、键盘导航、语义化 HTML
   - 交互：hover/focus 效果是否自然
4. 发现问题则修复
5. 生成 deliverable.md 文档，包含：
   - 项目概述：实现了什么页面和功能
   - 技术栈：Next.js + Tailwind CSS + shadcn/ui
   - 项目结构：目录和文件说明
   - 如何运行：启动说明（npm install && npm run dev）
   - 页面列表：每个页面的路由和功能说明
   - 设计说明：使用的设计风格和配色方案

这是交付给其他团队成员的文档，请写得清晰、专业。"""

        # 注入 Skills 设计规范用于审查
        skills_context = self._load_skills_context()
        context_parts = []
        if skills_context:
            context_parts.append(f"## 设计规范（审查标准）\n\n{skills_context}")
        if analysis:
            context_parts.append(f"## 需求分析\n\n{analysis}")
        if requirement:
            context_parts.append(f"## 原始需求\n\n{requirement}")

        result = await self.claude_code.execute(
            task=task,
            context="\n\n".join(context_parts) if context_parts else None,
            working_dir=client_dir,
        )

        if result.success:
            # 读取生成的摘要文档
            deliverable_path = os.path.join(client_dir, "deliverable.md")
            if os.path.exists(deliverable_path):
                with open(deliverable_path, "r", encoding="utf-8") as f:
                    deliverable_content = f.read()
            else:
                deliverable_content = self._extract_output(result)

            # 生成完整的交付摘要
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
                    "action": "review_and_polish",
                    "client_name": client_name,
                    "client_dir": client_dir,
                    "deliverable_file": deliverable_path,
                },
            )
        else:
            return Message(
                content=f"审查打磨失败：{result.error}",
                cause_by="error",
                sent_from=self.agent_id,
                metadata={"action": "review_and_polish", "error": result.error},
            )

    # ========== 辅助方法 ==========

    def _extract_client_name(self, requirement: str) -> str:
        """从需求中提取客户名称"""
        import re
        patterns = [
            r"公司[：:]\s*(\S+)",
            r"客户[：:]\s*(\S+)",
            r"项目[：:]\s*(\S+)",
            r"产品[：:]\s*(\S+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, requirement)
            if match:
                return match.group(1)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"frontend_{timestamp}"

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
        """生成完整的交付摘要"""
        return f"""# 前端交付摘要

## 客户信息
- 客户名称：{client_name}
- 项目目录：{client_dir}
- 完成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- 技术栈：Next.js + Tailwind CSS + shadcn/ui

## 成果文档

{deliverable}

## 交付说明

前端项目代码已部署在服务器 `{client_dir}` 目录下。
运行方式：`cd {client_dir} && npm install && npm run dev`

其他团队成员可根据成果文档进行后续工作（部署、运维、财务核算等）。
"""
