"""
客服 Agent - 企业微信客户服务

职责：
1. 通过企业微信与客户沟通
2. 回答客户关于项目进度的问题（PostgreSQL）
3. 回答客户关于项目使用的问题（product.md）
4. 回答公司通用问题（FAQ）
5. 无法回答时诚实转人工（飞书通知老板）

设计：
- 无状态处理，有状态存储（每客户独立会话）
- 会话上下文存储在 Redis（快速读写）
- 对话历史持久化到 PostgreSQL
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agents.base import BaseAgent
from channels.wecom import WecomClient
from communication.queue import CSMessageQueue, QueuedMessage
from models.agent import ActionDef, AgentRole, ReactMode
from models.customer import CustomerSession
from models.message import Message
from storage.database import Database
from tools.llm.client import LLMClient

logger = logging.getLogger(__name__)

# 客服 Agent 角色定义
CS_ROLE = AgentRole(
    name="customer_service",
    profile="专业的客服代表",
    goal="为客户提供关于项目进度、使用方法的准确回答，无法回答时诚实转人工",
    constraints="""约束条件：
1. 只回答有确切信息来源的问题（数据库、product.md、FAQ）
2. 涉及承诺、报价、决策的问题必须转人工
3. 不要编造信息，不确定就说不确定
4. 语气友好专业，简洁明了""",
    tools=["wecom", "database", "product_doc", "faq"],
    actions=[
        ActionDef(
            name="classify_intent",
            description="判断客户消息意图",
        ),
        ActionDef(
            name="answer_project_progress",
            description="回答项目进度问题",
        ),
        ActionDef(
            name="answer_project_usage",
            description="回答项目使用问题",
        ),
        ActionDef(
            name="answer_feature_request",
            description="处理新功能需求",
        ),
        ActionDef(
            name="answer_faq",
            description="回答公司通用问题",
        ),
        ActionDef(
            name="escalate_to_human",
            description="转人工处理",
        ),
    ],
    watch={"customer_message"},
    react_mode=ReactMode.REACT,
    max_react_loop=1,
)

# 意图分类
INTENTS = [
    "project_progress",   # 项目进度
    "project_usage",      # 项目使用方法
    "feature_request",    # 新功能需求
    "company_faq",        # 公司通用问题
    "greeting",           # 打招呼
    "unknown",            # 无法识别
]

# 会话 Redis key 前缀
SESSION_KEY_PREFIX = "cs:session:"


class CustomerServiceAgent(BaseAgent):
    """客服 Agent"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        message_bus: Any = None,
        wecom_client: Optional[WecomClient] = None,
        db: Optional[Database] = None,
        redis_client: Any = None,
        cs_queue: Optional[CSMessageQueue] = None,
        feishu_client: Any = None,
        memory_manager: Any = None,
        input_guard: Any = None,
        **kwargs,
    ):
        super().__init__(
            role=CS_ROLE,
            llm_client=llm_client,
            message_bus=message_bus,
            memory_manager=memory_manager,
            **kwargs,
        )
        self.wecom = wecom_client
        self.db = db
        self.redis = redis_client
        self.cs_queue = cs_queue
        self.feishu = feishu_client  # 用于转人工通知老板
        self.input_guard = input_guard  # 输入校验器

        # 加载知识文件和人格文件
        self.product_doc = self._load_markdown("product.md")
        self.soul_doc = self._load_markdown("soul.md")

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

    # ========== 主入口 ==========

    async def handle_message(self, message: QueuedMessage) -> None:
        """
        处理客户消息（Queue Worker 调用）

        Args:
            message: 队列中的消息
        """
        customer_id = message.customer_id
        text = message.text
        customer_name = message.customer_name
        response_url = message.metadata.get("response_url", "")  # 智能机器人的回复 URL

        logger.info(f"客服 Agent 收到消息: customer={customer_id}, text={text[:50]}...")

        # 输入安全校验
        if self.input_guard:
            result = self.input_guard.validate(text, source="wecom_cs")
            if not result.allowed:
                logger.warning(f"[InputGuard] 客服消息被拒绝: {result.reason}")
                await self._send_reply(customer_id, "抱歉，您的消息无法处理。")
                if self.audit_logger:
                    await self.audit_logger.log_input_rejected(
                        text, "wecom_cs", result.reason, result.flags, self.agent_id,
                    )
                return
            text = result.content

        try:
            # 1. 加载会话上下文
            session = await self._load_session(customer_id)
            if customer_name and not session.customer_name:
                session.customer_name = customer_name

            # 2. 意图分类
            intent = await self._classify_intent(text, session)
            logger.info(f"意图分类: customer={customer_id}, intent={intent}")

            # 3. 根据意图处理
            if intent == "project_progress":
                answer = await self._handle_project_progress(customer_id, text, session)
            elif intent == "project_usage":
                answer = await self._handle_project_usage(customer_id, text, session)
            elif intent == "feature_request":
                answer = await self._handle_feature_request(customer_id, text, session)
            elif intent == "company_faq":
                answer = await self._handle_company_faq(text)
            elif intent == "greeting":
                answer = await self._handle_greeting(session)
            else:
                answer = await self._escalate_to_human(customer_id, session.customer_name, text)
                await self._send_reply(customer_id, answer)
                await self._save_session(session, text, answer)
                return

            # 4. 发送回答
            await self._send_reply(customer_id, answer, response_url)

            # 5. 更新会话历史
            await self._save_session(session, text, answer)

            # 6. 归档对话到三层记忆系统
            await self._archive_conversation(customer_id, session.customer_name, text, answer)

            # 7. 撤回排队提示（如果有）
            if self.cs_queue:
                notify_msg_id = await self.cs_queue.get_and_clear_notify_msg_id(customer_id)
                if notify_msg_id and self.wecom:
                    try:
                        await self.wecom.recall(notify_msg_id)
                    except Exception as e:
                        logger.warning(f"撤回排队提示失败: {e}")

        except Exception as e:
            logger.error(f"处理客户消息异常: customer={customer_id}, error={e}", exc_info=True)
            try:
                await self._send_reply(customer_id, "抱歉，系统暂时出了点问题，请稍后再试。")
            except Exception:
                pass

    # ========== 意图分类 ==========

    async def _classify_intent(self, text: str, session: CustomerSession) -> str:
        """LLM 判断客户意图"""
        # 快捷关键词匹配
        progress_keywords = ["进度", "状态", "进展", "做完", "完成", "开发完", "上线"]
        if any(kw in text for kw in progress_keywords):
            return "project_progress"

        usage_keywords = ["怎么用", "如何", "怎么改", "怎么设置", "在哪里", "后台", "操作", "教程"]
        if any(kw in text for kw in usage_keywords):
            return "project_usage"

        feature_keywords = ["能不能", "可以加", "支持", "新功能", "添加", "实现"]
        if any(kw in text for kw in feature_keywords):
            return "feature_request"

        greeting_keywords = ["你好", "hi", "hello", "在吗", "在不在"]
        if any(kw in text.lower() for kw in greeting_keywords):
            return "greeting"

        # LLM 分类
        if not self.llm:
            return "unknown"

        system_prompt = f"""你是一个意图分类器。根据客户消息，判断意图类别。

可选意图：
- project_progress: 客户想了解项目进度、状态
- project_usage: 客户想了解如何使用项目、操作方法
- feature_request: 客户提出新功能需求或询问是否支持某功能
- company_faq: 客户询问公司服务、价格、流程等通用问题
- greeting: 打招呼、寒暄
- unknown: 无法识别

只输出意图类别，不要输出其他内容。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"客户消息：{text}"},
        ]

        try:
            response = await self.llm.chat(messages, temperature=0.1, max_tokens=50)
            response = response.strip().lower()
            for intent in INTENTS:
                if intent in response:
                    return intent
        except Exception as e:
            logger.error(f"意图分类失败: {e}")

        return "unknown"

    # ========== 项目进度查询 ==========

    async def _handle_project_progress(
        self, customer_id: str, text: str, session: CustomerSession
    ) -> str:
        """回答项目进度问题（数据来源：PostgreSQL）"""
        if not self.db:
            return "抱歉，暂时无法查询项目进度，请稍后再试。"

        try:
            # 查询客户关联的项目
            projects = await self.db.fetch(
                """SELECT p.id, p.name, p.status, p.created_at
                   FROM projects p
                   JOIN cs_customer_projects cp ON p.id = cp.project_id
                   WHERE cp.external_userid = $1
                   ORDER BY p.created_at DESC""",
                customer_id,
            )

            if not projects:
                return "您好，暂时没有找到与您关联的项目信息。如有疑问，请联系老板确认。"

            # 查询各项目的任务进度
            progress_lines = []
            for project in projects:
                pid = project["id"]
                pname = project["name"]
                pstatus = project["status"]

                tasks = await self.db.fetch(
                    "SELECT title, status FROM tasks WHERE project_id = $1",
                    pid,
                )

                total = len(tasks)
                completed = sum(1 for t in tasks if t.get("status") == "completed")

                progress_lines.append(
                    f"项目「{pname}」：{pstatus}，任务进度 {completed}/{total}"
                )

            progress_text = "\n".join(progress_lines)

            # LLM 组织自然语言回答
            if self.llm:
                system_prompt = """你是客服，根据项目进度信息回答客户问题。
用简洁友好的语气回复，不要使用过于技术化的术语。"""

                llm_messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"客户问：{text}\n\n项目进度：\n{progress_text}"},
                ]
                return await self.llm.chat(llm_messages, temperature=0.5, max_tokens=300)

            return progress_text

        except Exception as e:
            logger.error(f"查询项目进度失败: {e}")
            return "抱歉，查询项目进度时出现问题，请稍后再试。"

    # ========== 项目使用查询 ==========

    async def _handle_project_usage(
        self, customer_id: str, text: str, session: CustomerSession
    ) -> str:
        """回答项目使用问题（数据来源：product.md）"""
        product_doc = await self._load_product_doc(session.customer_name, customer_id)

        if not product_doc:
            return "抱歉，暂时没有找到您项目的使用文档。我会通知老板，稍后为您提供说明。"

        if not self.llm:
            return "抱歉，系统暂时无法处理您的问题，请稍后再试。"

        system_prompt = """你是客服，根据产品文档回答客户的使用问题。

回答要求：
1. 只根据文档内容回答，不要编造
2. 如果文档中有明确的操作步骤，直接告诉客户
3. 如果文档中没有相关信息，诚实说不确定，建议客户问老板
4. 语气友好、简洁"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"客户问：{text}\n\n产品文档：\n{product_doc}"},
        ]

        try:
            return await self.llm.chat(messages, temperature=0.3, max_tokens=500)
        except Exception as e:
            logger.error(f"LLM 回答使用问题失败: {e}")
            return "抱歉，处理您的问题时出现异常，请稍后再试。"

    # ========== 新功能需求 ==========

    async def _handle_feature_request(
        self, customer_id: str, text: str, session: CustomerSession
    ) -> str:
        """处理新功能需求（数据来源：product.md）"""
        product_doc = await self._load_product_doc(session.customer_name, customer_id)

        if not product_doc:
            # 没有 product.md，直接转人工
            return await self._escalate_to_human(
                customer_id, session.customer_name,
                f"客户询问新功能：{text}（无产品文档）"
            )

        if not self.llm:
            return await self._escalate_to_human(
                customer_id, session.customer_name,
                f"客户询问新功能：{text}（LLM 不可用）"
            )

        system_prompt = """你是客服，根据产品文档处理客户的新功能需求。

判断规则：
1. 如果功能已经在"已实现功能"中 → 告诉客户怎么使用
2. 如果功能在"待实现"列表中 → 告诉客户这个功能在规划中，会反馈给老板
3. 如果功能不在文档任何部分 → 不要承诺，说需要确认后回复

绝对不要：
- 承诺实现某功能
- 给出时间表或报价
- 编造文档中没有的功能"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"客户问：{text}\n\n产品文档：\n{product_doc}"},
        ]

        try:
            response = await self.llm.chat(messages, temperature=0.3, max_tokens=300)

            # 检查是否需要转人工（LLM 回复中包含不确定的内容）
            escalate_keywords = ["需要确认", "不确定", "问老板", "稍后回复"]
            if any(kw in response for kw in escalate_keywords):
                # 同时通知老板
                await self._notify_boss(
                    f"客户 {session.customer_name or customer_id} 询问新功能：{text}"
                )

            return response

        except Exception as e:
            logger.error(f"处理功能需求失败: {e}")
            return await self._escalate_to_human(
                customer_id, session.customer_name,
                f"客户询问新功能：{text}"
            )

    # ========== 公司 FAQ ==========

    async def _handle_company_faq(self, text: str) -> str:
        """回答公司通用问题（数据来源：KnowledgeMemory 语义检索 + FAQ 表 + product.md）"""
        if not self.llm:
            return "抱歉，这个问题我需要确认一下，稍后让老板回复您。"

        try:
            # 构建系统提示词，包含知识文件
            system_parts = ["你是 MuZhu-AI 的客服，负责回答客户关于公司和服务的问题。"]

            if self.soul_doc:
                system_parts.append(f"\n## 你的人格定义\n{self.soul_doc}")

            if self.product_doc:
                system_parts.append(f"\n## 公司和产品信息\n{self.product_doc}")

            # 优先从 KnowledgeMemory 语义检索 FAQ
            knowledge_context = ""
            if self.memory_manager:
                try:
                    knowledge_context = await self.get_context(
                        query=text,
                        strategy="semantic",
                        max_tokens_estimate=1000,
                    )
                    if knowledge_context and "无相关记忆" not in knowledge_context:
                        system_parts.append(f"\n## 知识库检索结果\n{knowledge_context}")
                except Exception as e:
                    logger.warning(f"KnowledgeMemory 检索失败: {e}")

            # 回退：从 FAQ 表获取答案
            if self.db:
                faqs = await self.db.fetch(
                    "SELECT question, answer FROM cs_faq ORDER BY id"
                )
                if faqs:
                    faq_text = "\n".join([f"Q: {f['question']}\nA: {f['answer']}" for f in faqs])
                    system_parts.append(f"\n## FAQ 列表\n{faq_text}")

            system_parts.append("""
## 回答规则
1. 优先使用 FAQ 中的答案
2. 如果 FAQ 中没有，使用公司和产品信息回答
3. 如果问题超出你的能力范围（如报价、决策），说"这个问题我需要确认一下，稍后让老板回复您"
4. 回答简洁明了，不要过于冗长""")

            system_prompt = "\n".join(system_parts)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"客户问：{text}"},
            ]

            return await self.llm.chat(messages, temperature=0.3, max_tokens=500)

        except Exception as e:
            logger.error(f"回答问题失败: {e}")
            return "抱歉，这个问题我需要确认一下，稍后让老板回复您。"

    # ========== 打招呼 ==========

    async def _handle_greeting(self, session: CustomerSession) -> str:
        """处理打招呼"""
        name = session.customer_name or ""
        if name:
            return f"您好，{name}！有什么可以帮您的吗？"
        return "您好！有什么可以帮您的吗？"

    # ========== 转人工 ==========

    async def _escalate_to_human(
        self, customer_id: str, customer_name: str, question: str
    ) -> str:
        """
        转人工处理

        1. 回复客户：告知会转给老板
        2. 记录转人工记录到数据库
        3. 通知老板（飞书）
        """
        # 记录到数据库
        if self.db:
            try:
                await self.db.execute(
                    """INSERT INTO cs_escalations (external_userid, customer_name, question)
                       VALUES ($1, $2, $3)""",
                    customer_id, customer_name, question,
                )
            except Exception as e:
                logger.error(f"记录转人工失败: {e}")

        # 通知老板
        await self._notify_boss(f"客户 {customer_name or customer_id} 需要人工回复：\n{question}")

        return "这个问题我需要确认一下，稍后让老板回复您，请稍等。"

    # ========== 通知老板 ==========

    async def _notify_boss(self, message: str) -> None:
        """通过飞书通知老板"""
        if not self.feishu:
            logger.warning(f"飞书客户端未初始化，无法通知老板: {message}")
            return

        try:
            # 发送到 CEO 的飞书（使用默认 chat_id）
            # 这里需要老板的飞书 ID，暂时用日志记录
            logger.info(f"[转人工通知] {message}")
            # TODO: 实际发送飞书消息给老板
            # await self.feishu.send_to_user(boss_open_id, f"[客服转人工] {message}")
        except Exception as e:
            logger.error(f"飞书通知老板失败: {e}")

    # ========== 会话管理 ==========

    async def _load_session(self, customer_id: str) -> CustomerSession:
        """加载客户会话（Redis 优先，回退到 PostgreSQL）"""
        # 尝试从 Redis 加载
        if self.redis:
            try:
                key = f"{SESSION_KEY_PREFIX}{customer_id}"
                data = await self.redis.get(key)
                if data:
                    return CustomerSession.from_dict(json.loads(data))
            except Exception as e:
                logger.warning(f"从 Redis 加载会话失败: {e}")

        # 从 PostgreSQL 加载历史
        history = []
        if self.db:
            try:
                rows = await self.db.fetch(
                    """SELECT role, content FROM cs_conversations
                       WHERE external_userid = $1
                       ORDER BY created_at DESC
                       LIMIT 20""",
                    customer_id,
                )
                # 反转为时间正序
                rows = list(reversed(rows))
                for i in range(0, len(rows) - 1, 2):
                    if rows[i]["role"] == "customer" and i + 1 < len(rows) and rows[i + 1]["role"] == "agent":
                        history.append({
                            "customer": rows[i]["content"],
                            "agent": rows[i + 1]["content"],
                            "time": "",
                        })
            except Exception as e:
                logger.warning(f"从 PostgreSQL 加载历史失败: {e}")

        # 查询客户关联的项目
        project_ids = []
        if self.db:
            try:
                projects = await self.db.fetch(
                    "SELECT project_id FROM cs_customer_projects WHERE external_userid = $1",
                    customer_id,
                )
                project_ids = [p["project_id"] for p in projects]
            except Exception as e:
                logger.warning(f"查询客户项目失败: {e}")

        session = CustomerSession(
            customer_id=customer_id,
            project_ids=project_ids,
            history=history[-10:],  # 最多保留 10 轮对话
        )

        return session

    async def _save_session(
        self, session: CustomerSession, customer_msg: str, agent_reply: str
    ) -> None:
        """保存会话（更新 Redis + 持久化到 PostgreSQL）"""
        # 更新会话历史
        session.add_round(customer_msg, agent_reply)

        # 保存到 Redis
        if self.redis:
            try:
                key = f"{SESSION_KEY_PREFIX}{session.customer_id}"
                await self.redis.set(
                    key,
                    json.dumps(session.to_dict()),
                    ex=86400,  # 24 小时过期
                )
            except Exception as e:
                logger.warning(f"保存会话到 Redis 失败: {e}")

        # 持久化到 PostgreSQL
        if self.db:
            try:
                await self.db.execute(
                    """INSERT INTO cs_conversations (external_userid, role, content)
                       VALUES ($1, 'customer', $2)""",
                    session.customer_id, customer_msg,
                )
                await self.db.execute(
                    """INSERT INTO cs_conversations (external_userid, role, content)
                       VALUES ($1, 'agent', $2)""",
                    session.customer_id, agent_reply,
                )
            except Exception as e:
                logger.warning(f"保存对话到 PostgreSQL 失败: {e}")

    # ========== 记忆系统集成 ==========

    async def _archive_conversation(
        self, customer_id: str, customer_name: str, customer_msg: str, agent_reply: str
    ) -> None:
        """
        归档对话到三层记忆系统（增量接入，不破坏现有会话存储）
        """
        if not self.memory_manager:
            return

        try:
            # 存储到 SessionMemory（Layer 2）
            await self.remember(
                content=f"客户 {customer_name or customer_id}: {customer_msg}\n客服: {agent_reply}",
                memory_type="chat",
                metadata={
                    "customer_id": customer_id,
                    "customer_name": customer_name,
                    "chat_id": f"cs_{customer_id}",
                    "messages": [
                        {"role": "customer", "content": customer_msg},
                        {"role": "agent", "content": agent_reply},
                    ],
                },
            )
        except Exception as e:
            logger.warning(f"归档对话到记忆系统失败: {e}")

    # ========== product.md 读取 ==========

    def _safe_path(self, base_dir: str, relative: str) -> str:
        """验证路径在 base_dir 内，防止路径遍历攻击"""
        resolved = os.path.realpath(os.path.join(base_dir, relative))
        base_real = os.path.realpath(base_dir)
        if not resolved.startswith(base_real + os.sep):
            raise ValueError(f"[Security] 路径遍历被阻止: {relative}")
        return resolved

    async def _load_product_doc(self, customer_name: str, customer_id: str) -> str:
        """
        读取客户项目的 product.md

        查找顺序：
        1. /root/{customer_name}/product.md
        2. 从数据库查询项目路径
        """
        # 直接用客户名查找
        if customer_name:
            try:
                path = self._safe_path("/root", f"{customer_name}/product.md")
                if os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            content = f.read()
                        logger.info(f"已加载 product.md: {path}")
                        return content
                    except Exception as e:
                        logger.error(f"读取 product.md 失败: {path}, error={e}")
            except ValueError as e:
                logger.warning(str(e))

        # 从数据库查询项目路径
        if self.db:
            try:
                projects = await self.db.fetch(
                    """SELECT p.name FROM projects p
                       JOIN cs_customer_projects cp ON p.id = cp.project_id
                       WHERE cp.external_userid = $1""",
                    customer_id,
                )
                for p in projects:
                    try:
                        path = self._safe_path("/root", f"{p['name']}/product.md")
                        if os.path.exists(path):
                            try:
                                with open(path, "r", encoding="utf-8") as f:
                                    return f.read()
                            except Exception as e:
                                logger.error(f"读取 product.md 失败: {path}, error={e}")
                    except ValueError as e:
                        logger.warning(str(e))
                        continue
            except Exception as e:
                logger.warning(f"查询项目路径失败: {e}")

        return ""

    # ========== 发送回复 ==========

    async def _send_reply(self, customer_id: str, text: str, response_url: str = "") -> None:
        """通过企业微信发送回复"""
        if not self.wecom:
            logger.warning(f"企业微信客户端未初始化，消息未发送: {text[:50]}")
            return

        try:
            await self.wecom.send_text_to_external(customer_id, text, response_url)
        except Exception as e:
            logger.error(f"企业微信发送回复失败: {e}")

    # ========== BaseAgent 接口实现 ==========

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """思考阶段（客服 Agent 主要通过 handle_message 处理，此方法处理内部消息）"""
        if not messages:
            return None

        last_message = messages[-1]
        if last_message.cause_by == "customer_message":
            return self.role.get_action_by_name("classify_intent")

        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        """执行阶段"""
        return Message(
            content="客服 Agent 已处理",
            cause_by="cs_action",
            sent_from=self.agent_id,
        )
