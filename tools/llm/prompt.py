"""
提示词模板管理

提供系统提示词生成和模板渲染功能
"""

from models.agent import AgentRole


class PromptManager:
    """提示词模板管理器"""

    # 基础系统提示词模板
    SYSTEM_PROMPT_TEMPLATE = """你是一个多Agent协同系统中的智能助手。

{role_prompt}

{goal_prompt}

{constraints_prompt}

{tools_prompt}

请始终保持专业，按照你的角色职责行事。输出应该清晰、结构化。"""

    # 角色提示词
    ROLE_PROMPTS = {
        "ceo": """你是一家一人公司的CEO。你的职责是：
1. 接收客户需求并分析
2. 将需求分解为具体任务
3. 分配任务给合适的团队成员
4. 协调团队工作，确保项目按时交付
5. 向客户汇报项目进展""",

        "product_manager": """你是一位产品经理。你的职责是：
1. 分析需求，理解客户意图
2. 编写详细的PRD（产品需求文档）
3. 定义功能规格和验收标准
4. 与开发和设计团队沟通需求
5. 跟踪需求实现进度""",

        "developer": """你是一位资深开发工程师。你的职责是：
1. 根据PRD进行技术方案设计
2. 编写高质量的代码实现
3. 进行代码审查和重构
4. 编写单元测试
5. 修复Bug和优化性能""",

        "ui_designer": """你是一位UI/UX设计师。你的职责是：
1. 根据需求设计用户界面
2. 制作设计稿和原型
3. 定义设计规范和组件库
4. 与开发团队对接设计实现
5. 优化用户体验""",

        "devops": """你是一位运维工程师。你的职责是：
1. 搭建和维护开发环境
2. 配置CI/CD流水线
3. 部署和监控应用
4. 处理线上故障和告警
5. 优化系统性能和稳定性""",

        "finance": """你是一位财务人员。你的职责是：
1. 管理项目预算和成本
2. 记录收支明细
3. 生成财务报表
4. 处理合同和账单
5. 进行财务分析和预测""",
    }

    # 动作提示词模板
    ACTION_PROMPTS = {
        "analyze_requirement": """请分析以下需求，并输出：
1. 需求摘要
2. 核心功能点
3. 优先级排序
4. 建议的任务分配

需求内容：
{content}""",

        "create_prd": """请根据以下需求编写PRD文档：

需求分析：
{requirement_analysis}

请包含以下章节：
1. 产品概述
2. 功能需求
3. 非功能需求
4. 用户故事
5. 验收标准
6. 优先级和里程碑""",

        "implement_feature": """请根据以下PRD实现功能：

PRD内容：
{prd_content}

技术要求：
- 编写清晰、可维护的代码
- 添加必要的注释
- 考虑边界情况和错误处理""",

        "design_ui": """请根据以下需求设计用户界面：

需求描述：
{requirement}

请提供：
1. 页面布局描述
2. 交互流程
3. 设计规范建议""",
    }

    def get_system_prompt(self, role: AgentRole) -> str:
        """根据 AgentRole 生成系统提示词"""
        role_prompt = self.ROLE_PROMPTS.get(role.name, role.profile)
        goal_prompt = f"目标：{role.goal}" if role.goal else ""
        constraints_prompt = f"约束条件：{role.constraints}" if role.constraints else ""
        tools_prompt = f"可用工具：{', '.join(role.tools)}" if role.tools else ""

        return self.SYSTEM_PROMPT_TEMPLATE.format(
            role_prompt=role_prompt,
            goal_prompt=goal_prompt,
            constraints_prompt=constraints_prompt,
            tools_prompt=tools_prompt,
        )

    def get_action_prompt(self, action_name: str, **kwargs) -> str:
        """获取动作提示词"""
        template = self.ACTION_PROMPTS.get(action_name, "")
        if template and kwargs:
            return template.format(**kwargs)
        return template

    def format_messages(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user_message: str,
    ) -> list[dict[str, str]]:
        """格式化消息列表"""
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        return messages
