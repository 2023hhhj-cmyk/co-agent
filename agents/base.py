"""
Agent 基类 - 融合 MetaGPT 角色机制

核心设计（借鉴 MetaGPT）：
- Think-Act-Observe 三阶段循环
- watch 订阅机制：只处理关心的消息
- cause_by 标记：消息来源追踪
- 反应模式：REACT / BY_ORDER / PLAN_AND_ACT
- 三层记忆系统：WorkingMemory + SessionMemory + KnowledgeMemory
"""

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from analytics import emit_event, hash_args, hash_id
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message, MessageQueue
from observability.metrics import (
    agent_state as agent_state_metric,
    task_duration as task_duration_metric,
    task_total,
    messages_processed,
)
from tools.llm.client import LLMClient, collect_usage
from tools.llm.prompt import PromptManager


def _classify_error(e: Exception) -> str:
    """把异常归到 8 个 error_code 枚举之一。"""
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "timeout" in msg or "timeout" in name:
        return "llm_timeout"
    if "json" in msg and ("parse" in msg or "decode" in msg):
        return "llm_json_parse_fail"
    if "sandbox" in msg and "degraded" in msg:
        return "sandbox_degraded"
    if "too long" in msg or "length" in msg or "too_long" in msg:
        return "input_too_long"
    if "cycle" in msg:
        return "workflow_cycle_detected"
    if "max" in msg and "step" in msg:
        return "agent_max_steps_exceeded"
    if "exit" in msg and "code" in msg:
        return "tool_exit_nonzero"
    return "unknown"

if TYPE_CHECKING:
    from memory.manager import MemoryManager
    from memory.working import WorkingMemory
    from security.access_control import PolicyEnforcer
    from security.audit import AuditLogger

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Agent 基类

    融合 MetaGPT 的 Role 设计：
    - role: 角色定义（name, profile, goal, constraints）
    - watch: 订阅的消息类型
    - react_mode: 反应模式
    - Think-Act-Observe: 核心执行循环
    """

    def __init__(
        self,
        role: AgentRole,
        llm_client: Optional[LLMClient] = None,
        memory_manager: Optional["MemoryManager"] = None,
        policy_enforcer: Any = None,
        audit_logger: Any = None,
    ):
        # 基础属性
        self.agent_id = str(uuid.uuid4())
        # 会话记忆的归属 id：fork 后仍指向原 agent，避免任务记忆碎片化
        self.memory_owner_id = self.agent_id
        self.role = role
        self.llm = llm_client
        self.prompt_manager = PromptManager()

        # 状态
        self.state = "idle"  # idle, busy, error
        self.created_at = datetime.now()

        # 消息和记忆
        self.message_queue: MessageQueue = MessageQueue()
        self.memory: List[Message] = []  # 工作记忆（向后兼容）

        # 三层记忆系统
        self.memory_manager: Optional["MemoryManager"] = memory_manager
        self._working_memory = None  # 懒初始化，由 memory_manager.get_working() 提供

        # 注入 LLM 客户端给 MemoryManager（用于自动提炼）
        if memory_manager and llm_client:
            memory_manager.set_llm_client(llm_client)

        # 安全模块
        self.policy_enforcer = policy_enforcer
        self.audit_logger = audit_logger

        # 动作状态
        self.todo: ActionDef | None = None
        self.state_index: int = 0  # BY_ORDER 模式下的状态索引

    # ========== 每任务隔离 ==========

    def fork(self, client_id: str = "") -> "BaseAgent":
        """为单个任务创建独立执行上下文：共享服务，隔离可变状态。

        不调用 __init__，因此不会新建 LLM 客户端或数据库连接。
        """
        shared = object.__new__(type(self))
        fresh = {
            "agent_id", "message_queue", "memory", "_working_memory",
            "state", "todo", "state_index",
        }
        shared.__dict__.update(
            {k: v for k, v in self.__dict__.items() if k not in fresh}
        )
        shared.agent_id = str(uuid.uuid4())
        # 记忆归属按「客户+角色」确定性生成：同客户同角色跨任务、跨重启稳定；
        # 不同客户落到不同 key，天然隔离。未指定客户时沿用原归属。
        if client_id:
            shared.memory_owner_id = str(uuid.uuid5(
                uuid.NAMESPACE_DNS, f"muzhu:{client_id}:{self.role.name}",
            ))
        else:
            shared.memory_owner_id = self.memory_owner_id
        shared.message_queue = MessageQueue()
        shared.memory = []
        shared._working_memory = None
        shared.state = "idle"
        shared.todo = None
        shared.state_index = 0
        return shared

    # ========== 属性 ==========

    @property
    def name(self) -> str:
        return self.role.name

    @property
    def is_idle(self) -> bool:
        """是否空闲"""
        return (
            self.state == "idle"
            and self.todo is None
            and self.message_queue.is_empty()
        )

    @property
    def working_memory(self):
        """当前任务工作记忆（懒初始化，由 MemoryManager 管理）"""
        if self._working_memory is None and self.memory_manager:
            self._working_memory = self.memory_manager.get_working(self.agent_id)
        return self._working_memory

    # ========== MetaGPT 三阶段循环 ==========

    async def _observe(self) -> List[Message]:
        """
        观察阶段：从消息队列读取消息

        只处理本次 run() 放入本地队列、且 watch 匹配的消息。
        """
        messages = await self.message_queue.pop_all()

        # 过滤：只保留 watch 匹配的消息
        if self.role.watch:
            filtered = []
            for msg in messages:
                if msg.cause_by in self.role.watch or not msg.cause_by:
                    filtered.append(msg)
            messages = filtered

        # 添加到记忆
        for msg in messages:
            self.memory.append(msg)
            # 同时写入三层记忆系统的短期工作区
            if self.working_memory is not None:
                self.working_memory.add(msg)

        return messages

    # ========== 自动分块生成（防 LLM 输出截断） ==========

    def _estimate_tokens(self, text: str) -> int:
        """ponytail: 估算 token 数 + 预估输出。中文 1 字 ≈ 1.5 token，输出按输入 × 4。"""
        chinese = sum(1 for c in text if "一" <= c <= "鿿")
        ascii_words = len(text.split()) - chinese
        input_tokens = int(chinese * 1.5 + ascii_words * 1.3)
        # ponytail: 输出预估 = 输入 × 4（生成任务输出远大于输入）
        return input_tokens * 4

    async def _smart_generate(
        self, system: str, user: str, max_tokens: int = 8000, temperature: float = 0.5
    ) -> str:
        """
        ponytail: 自动判断是否需要分块生成。
        - 小任务：单次 LLM 调用
        - 大任务：先调 LLM 拆子任务，再分别生成，最后合并
        """
        if not self.llm:
            raise RuntimeError("LLM not configured")

        full_prompt = f"{system}\n\n{user}"
        estimated = self._estimate_tokens(full_prompt)
        # ponytail: 阈值是 max_tokens / 4 —— 大多数生成任务输入 600 token 就要 4× 输出
        chunk_threshold = max_tokens // 4
        logger.info(f"DEBUG _smart_generate: estimated={estimated} threshold={chunk_threshold}")
        if estimated <= chunk_threshold:
            logger.info("DEBUG _smart_generate: 单次调用（小任务）")
            return await self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        logger.info("DEBUG _smart_generate: 走分块路径（大任务）")

        # 大任务：拆分 + 增量生成
        import json
        decompose_prompt = (
            f"把以下任务拆成若干子任务，每个子任务的目标输出 ≤ {max_tokens} token。\n"
            f"输出严格 JSON 数组，每个元素形如 {{\"name\": \"<短名>\", \"desc\": \"<子任务描述>\"}}：\n\n"
            f"{user}"
        )
        try:
            decomp_raw = await self.llm.chat(
                [{"role": "user", "content": decompose_prompt}],
                temperature=0.3,
                max_tokens=500,
            )
            # ponytail: 容忍 markdown code fence 包裹
            decomp_raw = decomp_raw.strip()
            if decomp_raw.startswith("```"):
                decomp_raw = decomp_raw.split("\n", 1)[1].rsplit("```", 1)[0]
            sub_tasks = json.loads(decomp_raw)
        except Exception as e:
            # ponytail: 拆不动就降级——单次调用 + 提醒调用方可能截断
            logger.warning(f"任务拆解失败: {e}，降级单次调用")
            return await self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )

        # 第一步：生成骨架（含占位 `<!-- @module: name -->`）
        skeleton_prompt = (
            f"生成 HTML/代码骨架（head + 整体结构 + 各个 section 的占位）。\n"
            f"每个 section 用占位 `<!-- @module: <name> -->` 标记，便于后续填充。\n"
            f"目标输出 ≤ {max_tokens} token。\n\n任务：\n{user}"
        )
        skeleton = await self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": skeleton_prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # 后续：每个子任务填一个 section
        for sub in sub_tasks:
            placeholder = f"<!-- @module: {sub['name']} -->"
            if placeholder not in skeleton:
                logger.warning(f"骨架里没找到 {placeholder}，跳过")
                continue
            chunk_prompt = (
                f"只输出这一个 section 的源码（不要 head/body 标签）。\n"
                f"对应占位名：`{sub['name']}`\n"
                f"任务描述：{sub['desc']}\n"
                f"目标输出 ≤ {max_tokens} token。"
            )
            chunk = await self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": chunk_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            skeleton = skeleton.replace(placeholder, chunk)

        return skeleton

    @abstractmethod
    async def _think(self, messages: List[Message]) -> Optional[ActionDef]:
        """
        思考阶段：LLM 决策下一步动作

        子类实现具体的决策逻辑

        Args:
            messages: 观察到的消息

        Returns:
            要执行的动作，或 None（无需动作）
        """
        pass

    @abstractmethod
    async def _act(self, action: ActionDef, context: Optional[Dict[str, Any]] = None) -> Message:
        """
        执行阶段：执行动作，生成消息

        子类实现具体的执行逻辑

        Args:
            action: 要执行的动作
            context: 上下文信息

        Returns:
            执行结果消息
        """
        pass

    async def _react(self, messages: List[Message]) -> Optional[Message]:
        """
        REACT 模式：Think-Act 循环

        借鉴 MetaGPT 的反应模式：
        - 循环执行 Think-Act
        - 直到没有新动作或达到最大循环次数
        """
        import time as _t
        result = None
        for _ in range(self.role.max_react_loop):
            # 埋点：agent.think start
            _think_start = _t.time()
            emit_event("agent.think", agent_role=self.role.name, phase="start")
            try:
                # 用量按协程隔离收集，避免并发时读到别的请求的 usage
                with collect_usage() as _usage:
                    action = await self._think(messages)
                _u = _usage
                emit_event("agent.think", agent_role=self.role.name, phase="end",
                           duration_ms=int((_t.time() - _think_start) * 1000), success=True,
                           prompt_tokens=_u.get("prompt_tokens", 0),
                           completion_tokens=_u.get("completion_tokens", 0))
            except Exception:
                emit_event("agent.think", agent_role=self.role.name, phase="end",
                           duration_ms=int((_t.time() - _think_start) * 1000), success=False)
                raise
            if action is None:
                break

            # 埋点：agent.act start
            _act_start = _t.time()
            emit_event("agent.act", agent_role=self.role.name, phase="start",
                       action_name=action.name, action_args_hash=hash_args(action.model_dump()))
            try:
                # ponytail: 传 messages 让 _act 拿到 last_message（含 PRD 等上下文）
                with collect_usage() as _usage:
                    result = await self._act(action, context={"messages": messages})
                _u = _usage
                emit_event("agent.act", agent_role=self.role.name, phase="end",
                           action_name=action.name,
                           duration_ms=int((_t.time() - _act_start) * 1000), success=True,
                           prompt_tokens=_u.get("prompt_tokens", 0),
                           completion_tokens=_u.get("completion_tokens", 0))
            except Exception:
                emit_event("agent.act", agent_role=self.role.name, phase="end",
                           action_name=action.name,
                           duration_ms=int((_t.time() - _act_start) * 1000), success=False)
                raise
            # 将结果加入消息，供下一轮 Think 使用
            messages.append(result)
        return result

    async def _by_order(self, messages: List[Message]) -> Optional[Message]:
        """
        BY_ORDER 模式：按顺序执行动作列表

        借鉴 MetaGPT：
        - 按 actions 列表顺序执行
        - 每次执行一个动作
        """
        if not self.role.actions:
            return None

        if self.state_index >= len(self.role.actions):
            self.state_index = 0  # 重置
            return None

        action = self.role.actions[self.state_index]
        self.state_index += 1
        return await self._act(action, {"messages": messages})

    # ========== 主入口 ==========

    async def run(self, message: Optional[Message] = None) -> Optional[Message]:
        """
        主执行入口

        流程：
        1. 如果有输入消息，加入队列
        2. 执行 _observe() 获取消息
        3. 主动检索相关记忆，注入消息流
        4. 如果有消息，根据 react_mode 执行
        5. 返回结果
        """
        # 注入输入消息
        if message:
            await self.message_queue.push(message)

        # 观察
        messages = await self._observe()
        if not messages:
            return None

        # 主动检索相关记忆（P1: Proactive Recall）
        messages = await self._inject_memory_context(messages)

        # 权限检查：验证 Agent 是否有权处理此类消息
        if self.policy_enforcer:
            for msg in messages:
                if msg.cause_by and msg.cause_by != "memory_context":
                    if not self.policy_enforcer.can_watch(self.role.name, {msg.cause_by}):
                        logger.warning(f"[AccessControl] {self.name} 无权处理 {msg.cause_by}")
                        if self.audit_logger:
                            await self.audit_logger.log_auth_failure(
                                self.agent_id, msg.cause_by,
                                f"Agent {self.name} 无权处理该消息类型",
                            )
                        return None

        # 更新状态 + 指标
        self.state = "busy"
        agent_state_metric.labels(agent=self.name, role=self.role.name).set(1)

        # 消息处理计数
        for msg in messages:
            if msg.cause_by and msg.cause_by != "memory_context":
                messages_processed.labels(
                    agent=self.name, cause_by=msg.cause_by
                ).inc()

        import time
        task_start = time.time()
        result = None
        succeeded = False
        outcome = "success"
        try:
            # 根据反应模式执行
            if self.role.react_mode == ReactMode.REACT:
                result = await self._react(messages)
            elif self.role.react_mode == ReactMode.BY_ORDER:
                result = await self._by_order(messages)
            elif self.role.react_mode == ReactMode.PLAN_AND_ACT:
                result = await self._plan_and_act(messages)
            else:
                result = await self._react(messages)

            # 发布结果
            if result:
                await self.publish_message(result)

            succeeded = True
            # 返回错误 Message 而未抛异常的任务，同样记为失败
            if getattr(result, "cause_by", "") in ("error", "failed"):
                outcome = "failed"
            return result

        except Exception as e:
            logger.error(f"Agent {self.name} error: {e}")
            self.state = "error"
            agent_state_metric.labels(agent=self.name, role=self.role.name).set(2)
            task_total.labels(agent=self.name, status="error").inc()
            if self.audit_logger:
                try:
                    await self.audit_logger.log_task_executed(
                        self.agent_id, str(e)[:200], result="error", duration_ms=0,
                        agent_role=self.role.name,
                    )
                except Exception:
                    logger.exception("记录任务失败审计时出错")
            # 埋点：agent.error
            emit_event("agent.error", agent_role=self.role.name,
                       error_code=_classify_error(e),
                       error_msg_hash=hash_id(str(e)))
            raise
        finally:
            elapsed = time.time() - task_start
            self.state = "idle"
            self.todo = None
            agent_state_metric.labels(agent=self.name, role=self.role.name).set(0)
            task_duration_metric.labels(agent=self.name).observe(elapsed)
            if succeeded:
                task_total.labels(agent=self.name, status=outcome).inc()
            # 审计失败不改变任务执行结果。
            if succeeded and self.audit_logger and result:
                try:
                    await self.audit_logger.log_task_executed(
                        self.agent_id,
                        result.content[:200] if result.content else "",
                        result=outcome, agent_role=self.role.name,
                    )
                except Exception:
                    logger.exception("记录任务成功审计时出错")
            # 自动归档短期记忆到中期记忆
            if self.memory_manager and self.working_memory and not self.working_memory.is_empty:
                try:
                    await self.memory_manager.archive_working_memory(
                        agent_id=self.memory_owner_id,
                        agent_role=self.role.name,
                        working_memory=self.working_memory,
                    )
                    self.working_memory.clear()
                    if hasattr(self.memory_manager, "drop_working"):
                        self.memory_manager.drop_working(self.agent_id)
                    self._working_memory = None
                except Exception as e:
                    logger.warning(f"归档工作记忆失败: {e}")

    async def _plan_and_act(self, messages: List[Message]) -> Optional[Message]:
        """
        PLAN_AND_ACT 模式：先规划再执行

        借鉴 MetaGPT 的 Planner 模式
        """
        # TODO: 实现规划逻辑
        # 暂时回退到 REACT 模式
        return await self._react(messages)

    # ========== 消息发布 ==========

    async def publish_message(self, message: Message) -> None:
        """
        记录本轮输出。任务调度不经过这里，由 WorkflowExecutor 调用 run()。
        """
        message.sent_from = self.agent_id
        self.memory.append(message)

    def put_message(self, message: Message) -> None:
        """
        直接放入本地队列，供后续 run() 观察。
        """
        asyncio.create_task(self.message_queue.push(message))

    # ========== 记忆系统 API ==========

    async def recall(
        self,
        query: str,
        strategy: str = "default",
        top_k: int = 10,
    ) -> List[Any]:
        """
        检索相关记忆（三层融合）

        Args:
            query: 查询文本
            strategy: 检索策略 — "default" | "recent" | "semantic" | "comprehensive"
            top_k: 每层最大返回数

        Returns:
            List[MemoryItem]
        """
        if not self.memory_manager:
            return []
        return await self.memory_manager.recall(
            query=query,
            agent_id=self.memory_owner_id,
            agent_role=self.role.name,
            strategy=strategy,
            top_k=top_k,
        )

    async def remember(
        self,
        content: str,
        memory_type: str = "task",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        存储新记忆

        Args:
            content: 记忆内容
            memory_type: 类型 — "task" | "chat" | "experience" | "faq" | "best_practice"
            metadata: 额外元数据

        Returns:
            memory_id
        """
        if not self.memory_manager:
            return None
        return await self.memory_manager.remember(
            content=content,
            agent_id=self.memory_owner_id,
            agent_role=self.role.name,
            memory_type=memory_type,
            metadata=metadata,
        )

    async def get_context(
        self,
        query: str,
        strategy: str = "default",
        max_tokens_estimate: int = 2000,
    ) -> str:
        """
        获取完整上下文文本（用于注入 LLM prompt）

        Args:
            query: 当前查询
            strategy: 检索策略
            max_tokens_estimate: 最大估计 token 数

        Returns:
            格式化的上下文文本
        """
        if not self.memory_manager:
            return ""
        return await self.memory_manager.get_context(
            query=query,
            agent_id=self.memory_owner_id,
            agent_role=self.role.name,
            strategy=strategy,
            max_tokens_estimate=max_tokens_estimate,
        )

    # ========== 主动检索（P1: Proactive Recall）==========

    async def _inject_memory_context(self, messages: List[Message]) -> List[Message]:
        """
        在 Think 之前，自动检索相关记忆并注入消息流

        以最后一条消息内容作为查询，从三层记忆系统检索相关上下文，
        作为 memory_context 消息插入，供 _think() 使用。

        Args:
            messages: _observe() 返回的消息列表

        Returns:
            增强后的消息列表（含记忆上下文）
        """
        if not self.memory_manager or not messages:
            return messages

        try:
            # 以最后一条消息内容作为查询
            query = messages[-1].content if messages[-1].content else ""
            if not query or len(query) < 3:
                return messages

            # 检索相关上下文（限定 500 token 估算）
            context = await self.memory_manager.get_context(
                query=query,
                agent_id=self.memory_owner_id,
                agent_role=self.role.name,
                strategy="default",
                max_tokens_estimate=500,
            )

            # 如果有相关记忆，注入消息流
            if context and "无相关记忆" not in context:
                memory_msg = Message(
                    content=context,
                    cause_by="memory_context",
                    sent_from="memory_system",
                    send_to={self.agent_id},
                )
                # 插入到最后一条消息之前（让 Agent 看到记忆后再看最新消息）
                messages.insert(-1, memory_msg)
                logger.debug(
                    f"Agent {self.name}: 注入记忆上下文 ({len(context)} chars)"
                )

        except Exception as e:
            # 主动检索失败不影响主流程
            logger.warning(f"Agent {self.name} 主动检索记忆失败: {e}")

        return messages

    # ========== 辅助方法 ==========

    def get_memories(self, k: int = 10) -> List[Message]:
        """获取最近 k 条记忆"""
        return self.memory[-k:]

    def get_history_text(self, k: int = 10) -> str:
        """获取最近 k 条记忆的文本形式"""
        memories = self.get_memories(k)
        return "\n".join([f"[{m.sent_from}]: {m.content}" for m in memories])

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role.to_dict(),
            "state": self.state,
            "created_at": self.created_at.isoformat(),
            "memory_count": len(self.memory),
        }
