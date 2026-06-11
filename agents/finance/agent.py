"""
财务人员 Agent

职责：
1. 管理项目预算和成本
2. 记录收支明细
3. 生成财务报表
4. 处理合同和账单
5. 进行财务分析和预测
"""

import logging
from typing import Any, Optional

from agents.base import BaseAgent
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message
from tools.llm.client import LLMClient
from tools.llm.prompt import PromptManager

logger = logging.getLogger(__name__)

# 财务人员角色定义
FINANCE_ROLE = AgentRole(
    name="finance",
    profile="财务人员",
    goal="管理项目预算和成本，生成财务报表",
    constraints="确保财务数据准确，符合财务规范",
    tools=["accounting_system", "spreadsheet"],
    actions=[
        ActionDef(
            name="record_cost",
            description="记录项目成本",
            prompt_template="请记录以下项目成本：\n{project_info}",
        ),
        ActionDef(
            name="generate_invoice",
            description="生成账单",
            prompt_template="请根据以下信息生成账单：\n{billing_info}",
        ),
        ActionDef(
            name="financial_report",
            description="生成财务报表",
            prompt_template="请生成财务报表：\n{report_period}",
        ),
    ],
    watch={"deploy_complete", "project_complete"},
    react_mode=ReactMode.BY_ORDER,
)


class FinanceAgent(BaseAgent):
    """财务人员 Agent"""

    def __init__(self, llm_client: Optional[LLMClient] = None, message_bus: Any = None):
        super().__init__(
            role=FINANCE_ROLE,
            llm_client=llm_client,
            message_bus=message_bus,
        )

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """思考阶段"""
        if not messages:
            return None

        last_message = messages[-1]

        if last_message.cause_by == "deploy_complete":
            return self.role.get_action_by_name("record_cost")
        elif last_message.cause_by == "project_complete":
            return self.role.get_action_by_name("generate_invoice")

        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """执行阶段"""
        messages = context.get("messages", []) if context else []
        last_message = messages[-1] if messages else None

        if action.name == "record_cost":
            project_info = last_message.content if last_message else ""

            if self.llm:
                system_prompt = self.prompt_manager.get_system_prompt(self.role)
                action_prompt = self.prompt_manager.get_action_prompt(
                    "record_cost", project_info=project_info
                )

                messages_for_llm = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": action_prompt},
                ]

                cost_record = await self.llm.chat(messages_for_llm, temperature=0.3)
            else:
                cost_record = f"""# 成本记录

## 项目信息
{project_info[:200]}...

## 成本明细
- 人力成本：待核算
- 服务器成本：待核算
- 工具成本：待核算
- 总计：待核算
"""

            return Message(
                content=cost_record,
                cause_by="record_cost",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={"action": "record_cost"},
            )

        elif action.name == "generate_invoice":
            billing_info = last_message.content if last_message else ""

            if self.llm:
                system_prompt = self.prompt_manager.get_system_prompt(self.role)
                action_prompt = self.prompt_manager.get_action_prompt(
                    "generate_invoice", billing_info=billing_info
                )

                messages_for_llm = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": action_prompt},
                ]

                invoice = await self.llm.chat(messages_for_llm, temperature=0.3)
            else:
                invoice = f"""# 账单

## 客户信息
待填写

## 服务明细
- 项目开发：待核算
- 部署服务：待核算
- 维护服务：待核算

## 总计
待核算
"""

            return Message(
                content=invoice,
                cause_by="generate_invoice",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={"action": "generate_invoice"},
            )

        elif action.name == "financial_report":
            report_period = last_message.content if last_message else ""

            if self.llm:
                system_prompt = self.prompt_manager.get_system_prompt(self.role)
                action_prompt = self.prompt_manager.get_action_prompt(
                    "financial_report", report_period=report_period
                )

                messages_for_llm = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": action_prompt},
                ]

                report = await self.llm.chat(messages_for_llm, temperature=0.3)
            else:
                report = f"""# 财务报表

## 报告期间
{report_period}

## 收入
待统计

## 支出
待统计

## 利润
待计算
"""

            return Message(
                content=report,
                cause_by="financial_report",
                sent_from=self.agent_id,
                send_to={"*"},
                metadata={"action": "financial_report"},
            )

        else:
            return Message(
                content=f"未知动作：{action.name}",
                cause_by="error",
                sent_from=self.agent_id,
            )
