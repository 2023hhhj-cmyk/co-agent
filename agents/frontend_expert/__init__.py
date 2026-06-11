"""
前端专家 Agent

职责：
1. 分析前端需求，确定页面类型和风格
2. 初始化 Next.js + Tailwind CSS + shadcn/ui 项目
3. 生成高质量、可生产的前端页面
4. 自动审查和打磨设计质量
"""

from agents.frontend_expert.agent import FrontendExpertAgent, FRONTEND_EXPERT_ROLE

__all__ = ["FrontendExpertAgent", "FRONTEND_EXPERT_ROLE"]
