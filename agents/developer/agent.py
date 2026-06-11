"""
开发工程师 Agent

职责：
1. 根据客户需求，在服务器上为客户创建专属项目目录
2. 调用 Claude Code 构建实际产品（网站、应用等）
3. 生成成果摘要文档，交付给下游 Agent

工作流程：
- 收到客户 PRD → 创建客户目录 → Claude Code 构建 → 生成摘要文档
"""

import logging
import os
from datetime import datetime
from typing import Any, Optional

from agents.base import BaseAgent
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message
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
    ):
        super().__init__(
            role=DEVELOPER_ROLE,
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
            return await self._design_architecture(last_message)
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
