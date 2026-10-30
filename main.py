"""
Muzhu-AI 多Agent协同系统 - 主程序入口

融合架构：
- MetaGPT 角色机制（Think-Act-Observe）
- ChatDev 图工作流（DAG 拓扑排序）
"""

import asyncio
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded


# P2 #19 修复：限流 key 函数
# 默认用 client.host，企业 NAT 后所有员工共享同一 IP → 配额被极少数人耗尽
# 改用 X-Forwarded-For 头（需配合 nginx `set_header X-Forwarded-For $proxy_add_x_forwarded_for`）
# 无 XFF 时回退到 client.host
def _rate_limit_key(request: Request) -> str:
    """优先使用 X-Forwarded-For，否则回退 client.host"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # 取第一个 IP（最左为真实客户端）
        return xff.split(",")[0].strip()
    return get_remote_address(request)

from agents import (
    CEOAgent,
    CustomerServiceAgent,
    DeveloperAgent,
    DevOpsAgent,
    FrontendExpertAgent,
)
from channels.feishu import FeishuClient
from channels.wecom import WecomClient
from communication.queue import CSMessageQueue, QueueWorker, QueuedMessage
from config.settings import settings
from acceleration import ResponseCache, EmbeddingCache, PromptOptimizer
from memory import MemoryGraph, MemoryManager, SessionMemory, KnowledgeMemory
from models.message import Message
from observability import (
    setup_logging,
    generate_trace_id,
    HealthChecker,
    http_requests,
    http_request_duration,
    get_metrics,
    agent_state,
    sandbox_mode as sandbox_metric,
    queue_depth as queue_metric,
)
from security import InputGuard, SandboxedExecutor, PolicyEnforcer, AuditLogger
from storage.database import Database
from storage.state_manager import StateManager
from tools.deploy.customer_container import CustomerContainers
from tools.llm.router import LLMRouter
from tools.monitor.webhook_health import start_webhook_monitor

# 初始化结构化日志 (JSON + trace_id)
setup_logging(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== 全局组件 ==========

state_manager = StateManager()
database = Database()

# LLM 路由器
llm_router = LLMRouter()

# 记忆系统（三层架构：WorkingMemory + SessionMemory + KnowledgeMemory）
session_memory: SessionMemory = None
knowledge_memory: KnowledgeMemory = None
memory_manager: MemoryManager = None

# 安全系统
input_guard: InputGuard = None
sandbox: SandboxedExecutor = None
policy_enforcer: PolicyEnforcer = None
audit_logger: AuditLogger = None

# 可观测性
health_checker: HealthChecker = None

# 加速层
llm_cache: ResponseCache = None
embed_cache: EmbeddingCache = None

# 飞书客户端
feishu_client = FeishuClient()

# 企业微信客户端
wecom_ceo_client = WecomClient(
    corp_id=settings.WECOM_CEO_CORP_ID,
    agent_id=settings.WECOM_CEO_AGENT_ID,
    secret=settings.WECOM_CEO_SECRET,
    token=settings.WECOM_CEO_TOKEN,
    aes_key=settings.WECOM_CEO_AES_KEY,
)
wecom_cs_client = WecomClient(
    corp_id=settings.WECOM_CS_CORP_ID,
    agent_id=settings.WECOM_CS_AGENT_ID,
    secret=settings.WECOM_CS_SECRET,
    token=settings.WECOM_CS_TOKEN,
    aes_key=settings.WECOM_CS_AES_KEY,
)

# Agent 实例
agents = {}


def create_agents():
    """创建所有 Agent（注入 MemoryManager）"""
    global agents

    # 先创建下级 Agent（注入安全模块）
    devops_agent = DevOpsAgent(
        llm_client=llm_router.get_client_by_agent("devops"),
        memory_manager=memory_manager,
        policy_enforcer=policy_enforcer,
        audit_logger=audit_logger,
    )

    # 客户容器共享同一个实例：CEO 创建，Developer 在同一容器工作区里写文件
    customer_containers = CustomerContainers()

    agents = {
        "developer": DeveloperAgent(
            llm_client=llm_router.get_client_by_agent("developer"),
            memory_manager=memory_manager,
            policy_enforcer=policy_enforcer,
            audit_logger=audit_logger,
        ),
        "frontend_expert": FrontendExpertAgent(
            llm_client=llm_router.get_client_by_agent("frontend_expert"),
            memory_manager=memory_manager,
            policy_enforcer=policy_enforcer,
            audit_logger=audit_logger,
        ),
        "devops": devops_agent,
    }

    # CEO 最后创建，注入飞书客户端、团队、状态管理器和输入校验
    agents["ceo"] = CEOAgent(
        llm_client=llm_router.get_client_by_agent("ceo"),
        feishu_client=feishu_client,
        team=agents,
        memory_manager=memory_manager,
        state_manager=state_manager,
        input_guard=input_guard,
        policy_enforcer=policy_enforcer,
        audit_logger=audit_logger,
    )

    # 让 Developer 复用 CEO 的客户容器 API，而不是各自 new 一个
    for agent in agents.values():
        agent.customer_containers = customer_containers

    # 设置 DevOps Agent 的飞书客户端（用于告警通知）
    # 注意：需要 CEO Agent 的 chat_id，这里先设置客户端，chat_id 在首次消息时获取
    devops_agent._feishu_client = feishu_client

    return agents


# ========== 生命周期管理 ==========

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    logger.info("Starting Muzhu-AI Agent System...")

    # 连接存储
    await state_manager.connect()
    await database.connect()

    # P2 #18 修复：验证 Redis 持久化配置（消息队列依赖）
    # 若 Redis 未启用 AOF，进程崩溃时消息可能丢失
    try:
        aof_enabled = await state_manager.redis.config_get("appendonly")
        if aof_enabled.get("appendonly") != "yes":
            logger.warning(
                "[Security] Redis AOF 未启用 (appendonly=no)。"
                "消息队列持久化在崩溃场景下可能丢失。建议设置 appendonly=yes。"
            )
    except Exception as e:
        logger.warning(f"无法检查 Redis AOF 状态: {e}")

    # 初始化安全系统
    global input_guard, sandbox, policy_enforcer, audit_logger
    input_guard = InputGuard()
    # P2 #24 修复：Sandbox 初始化失败不应阻塞 app 启动
    try:
        sandbox = SandboxedExecutor(base_dir="/root")
    except Exception as e:
        logger.warning(f"[Security] 沙箱状态初始化失败（CLI 仍通过独立 Docker 执行器）: {e}")
        sandbox = None
    policy_enforcer = PolicyEnforcer()
    audit_logger = AuditLogger(database=database)
    logger.info(
        f"安全系统初始化完成: "
        f"InputGuard + Sandbox({sandbox.mode if sandbox else 'disabled'}) + "
        f"PolicyEnforcer + AuditLogger"
    )

    # 初始化可观测性
    global health_checker
    health_checker = HealthChecker(
        state_manager=state_manager,
        database=database,
        agents=agents,
    )
    # 设置初始沙箱模式指标
    sandbox_metric.set(1 if sandbox and sandbox.mode == "docker" else 0)
    logger.info("可观测性系统初始化完成")

    # 初始化加速层
    global llm_cache, embed_cache
    llm_cache = ResponseCache(state_manager=state_manager)
    embed_cache = EmbeddingCache(state_manager=state_manager)

    # 注入缓存到 LLM Router 的所有客户端
    for client in [llm_router.get_client_by_agent("ceo"),
                   llm_router.get_client_by_agent("developer"),
                   llm_router.get_client_by_agent("customer_service")]:
        if client:
            client.set_cache(llm_cache)
    logger.info("加速层初始化完成 (LLM缓存 + 嵌入缓存)")

    # 初始化三层记忆系统
    global session_memory, knowledge_memory, memory_manager
    session_memory = SessionMemory(state_manager=state_manager, database=database)
    try:
        knowledge_memory = KnowledgeMemory(database=database, embedding_cache=embed_cache)
        await knowledge_memory.setup()
        if knowledge_memory.is_ready:
            logger.info("KnowledgeMemory (PostgreSQL + pgvector) 已就绪")
        else:
            logger.warning("KnowledgeMemory 未就绪，语义检索不可用")
    except Exception as e:
        logger.warning(f"KnowledgeMemory 初始化失败，语义检索不可用: {e}")
        knowledge_memory = None
    memory_graph = MemoryGraph(state_manager=state_manager, database=database)
    memory_manager = MemoryManager(
        session_memory=session_memory,
        knowledge_memory=knowledge_memory,
        graph=memory_graph,
    )
    logger.info("三层记忆系统 + 关联图谱初始化完成")

    # 创建 Agent（P2 #24 修复：单个 Agent 失败不阻塞整个启动）
    try:
        create_agents()
        logger.info(f"Agents created: {list(agents.keys())}")
        devops = agents.get("devops")
        if devops:
            devops.bind_database(database)
            await devops.restore_deployments()
    except Exception as e:
        logger.error(f"[Critical] Agent 创建失败: {e}", exc_info=True)
        # 不 raise，让 app 至少能响应 /health 端点

    # 启动飞书 WebSocket 长连接
    async def handle_feishu_message(parsed: dict):
        """处理从飞书 WebSocket 收到的消息"""
        ceo = agents.get("ceo")
        if not ceo:
            return
        await ceo.handle_feishu_message(
            chat_id=parsed["chat_id"],
            text=parsed["text"],
            message_id=parsed["message_id"],
            sender_id=parsed.get("sender_id", ""),
            chat_type=parsed.get("chat_type", "p2p"),
        )

    feishu_client.set_message_handler(handle_feishu_message)

    # 卡片按钮回调处理
    def handle_feishu_card(data: dict):
        """处理飞书卡片按钮回调（通过 WebSocket）"""
        ceo = agents.get("ceo")
        if not ceo:
            return

        action = data.get("action", {})
        value = action.get("value", {})
        open_id = data.get("open_id", "")
        chat_id = data.get("open_chat_id", "")

        if value and chat_id:
            # P1 #10 修复：使用 uuid 而非 time() 生成唯一 message_id
            # 旧版 int(time()) 在毫秒内多次调用会撞车，且 get_event_loop() 在 3.10+ deprecated
            card_msg_id = f"card_{value.get('action', '')}_{value.get('plan_id', '')}_{uuid.uuid4().hex[:12]}"
            logger.info(f"卡片回调转发给 CEO: action={value.get('action')}")
            asyncio.run_coroutine_threadsafe(
                ceo.handle_feishu_message(
                    chat_id=chat_id,
                    text=json.dumps(value),
                    message_id=card_msg_id,
                    sender_id=open_id,
                ),
                asyncio.get_event_loop(),
            )

    feishu_client.set_card_handler(handle_feishu_card)
    feishu_client.start_ws(loop=asyncio.get_event_loop())
    logger.info("飞书 WebSocket 长连接已启动")

    # 初始化客服系统
    cs_queue = CSMessageQueue(state_manager.redis)
    cs_agent = CustomerServiceAgent(
        llm_client=llm_router.get_client_by_agent("customer_service"),
        wecom_client=wecom_cs_client,
        db=database,
        redis_client=state_manager.redis,
        cs_queue=cs_queue,
        feishu_client=feishu_client,
        memory_manager=memory_manager,
        input_guard=input_guard,
        policy_enforcer=policy_enforcer,
        audit_logger=audit_logger,
    )
    agents["customer_service"] = cs_agent

    # 启动队列 Worker
    cs_worker = QueueWorker(
        queue=cs_queue,
        handler=cs_agent.handle_message,
        batch_size=settings.CS_QUEUE_BATCH_SIZE,
        interval=settings.CS_QUEUE_INTERVAL,
    )
    asyncio.create_task(cs_worker.start())
    logger.info(f"客服系统已启动: batch_size={settings.CS_QUEUE_BATCH_SIZE}")

    logger.info("All systems initialized")

    # ponytail: 启动 Webhook 健康检查（每小时测一次 3 个回调 URL）
    try:
        monitor_task = start_webhook_monitor()
        if monitor_task:
            logger.info("Webhook monitor 已启动（每小时 1 次）")
    except Exception as e:
        logger.warning(f"Webhook monitor 启动失败: {e}")

    yield

    # 清理
    logger.info("Shutting down...")
    await feishu_client.close()
    await wecom_ceo_client.close()
    await wecom_cs_client.close()
    await llm_router.close_all()
    await state_manager.disconnect()
    await database.disconnect()


# ========== FastAPI 应用 ==========

app = FastAPI(
    title="Muzhu-AI Agent System",
    description="多Agent协同系统 - 融合 MetaGPT 角色 + ChatDev 图工作流",
    version="1.0.0",
    lifespan=lifespan,
)

# 速率限制器
limiter = Limiter(key_func=_rate_limit_key, default_limits=[settings.RATE_LIMIT])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, lambda request, exc: JSONResponse(
    status_code=429,
    content={"error": "Rate limit exceeded"},
))


# HTTP 指标中间件
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """追踪 HTTP 请求指标 + trace_id"""
    import time
    # 生成 trace_id
    tid = generate_trace_id()
    start = time.time()

    response = await call_next(request)

    duration = time.time() - start

    # 记录 HTTP 指标
    http_requests.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code,
    ).inc()

    http_request_duration.labels(
        method=request.method,
        endpoint=request.url.path,
    ).observe(duration)

    return response


# API Key 认证中间件（在 metrics 之后注册 = 先于 metrics 执行）
@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """API Key 认证中间件"""
    # 公开路径：无需认证
    public_paths = {"/", "/health", "/metrics"}
    # Webhook 路径：自有签名验证
    webhook_prefixes = {"/api/feishu/webhook", "/api/wecom/ceo/callback", "/api/wecom/cs/callback"}

    if request.url.path in public_paths:
        return await call_next(request)
    if any(request.url.path.startswith(p) for p in webhook_prefixes):
        return await call_next(request)

    # 缺少密钥时拒绝受保护接口，避免生产环境意外裸奔。
    if not settings.API_KEY:
        return JSONResponse(status_code=503, content={"error": "API authentication is not configured"})

    # 验证 X-API-Key
    api_key = request.headers.get("X-API-Key")
    if api_key != settings.API_KEY:
        if audit_logger:
            await audit_logger.log_auth_failure(
                agent_id="system",
                action=f"{request.method} {request.url.path}",
                reason="invalid or missing API key",
            )
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized", "message": "Valid X-API-Key header required"},
        )

    return await call_next(request)


# 安全 HTTP 头中间件
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """为所有响应添加安全 HTTP 头"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


# Body 大小限制中间件（P1 #14 修复）
# webhook 端点容易被大 body 攻击（slowapi 只按请求数限流）
MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MB

@app.middleware("http")
async def body_size_limit_middleware(request: Request, call_next):
    """拒绝超过 MAX_BODY_SIZE 的请求体，防 DoS"""
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_SIZE:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "Request body too large", "max_bytes": MAX_BODY_SIZE},
                    )
            except ValueError:
                pass
    return await call_next(request)


# ========== API 路由 ==========

@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "Muzhu-AI Agent System",
        "version": "1.0.0",
        "status": "running",
        "architecture": "MetaGPT Role + ChatDev Graph",
    }


@app.get("/health")
async def health():
    """增强健康检查 — 验证全部依赖"""
    if health_checker:
        return await health_checker.check()
    return {"status": "healthy", "note": "health_checker not initialized"}


@app.get("/metrics")
async def metrics():
    """Prometheus 指标端点"""
    from fastapi.responses import Response
    return Response(
        content=get_metrics(),
        media_type="text/plain; version=0.0.4",
    )


async def get_or_create_client(source: str, external_id: str, display_name: str = "") -> str:
    """第一次见到这个外部账号就分配 client_id，之后一直返回同一个。"""
    external_id = (external_id or "").strip()
    if not external_id:
        raise ValueError("missing external id")
    row = await database.fetchrow(
        "SELECT client_id FROM clients WHERE source = $1 AND external_id = $2",
        source, external_id,
    )
    if row:
        return row["client_id"]
    client_id = "c" + uuid.uuid4().hex[:12]
    await database.execute(
        "INSERT INTO clients (client_id, source, external_id, display_name) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (source, external_id) DO NOTHING",
        client_id, source, external_id, display_name or "",
    )
    row = await database.fetchrow(
        "SELECT client_id FROM clients WHERE source = $1 AND external_id = $2",
        source, external_id,
    )
    return row["client_id"]


@app.post("/api/requirement")
async def create_requirement(requirement: dict):
    """
    创建需求 - CEO 写需求文档并按任务调度执行

    流程：CEO 写需求文档、分配任务 → 按依赖分批派给执行器
    """
    content = requirement.get("content", "")
    if not content:
        return {"error": "Content is required"}
    external_id = str(requirement.get("external_id") or "").strip()
    source = str(requirement.get("source") or "api").strip() or "api"
    if not external_id:
        return {"error": "external_id is required"}
    client_id = await get_or_create_client(source, external_id, str(requirement.get("display_name") or ""))

    ceo = agents.get("ceo")
    if not ceo:
        return {"error": "CEO agent not available"}

    logger.info(f"收到需求: {content[:50]}...")

    try:
        # CEO 拆解任务
        plan = await ceo._decompose_tasks(content, client_id, "")
        if not plan:
            return {"status": "error", "message": "任务拆解失败"}

        # 执行（通过 WorkflowExecutor）
        await ceo._execute_plan(client_id, plan)

        return {
            "status": plan.status,
            "plan_id": plan.id,
            "tasks": [t.to_summary() for t in plan.tasks],
            "task_details": [{"id": t.id, "title": t.title, "status": t.status.value,
                              "error": t.error} for t in plan.tasks],
        }
    except Exception as e:
        logger.error(f"需求处理失败: {e}", exc_info=True)
        return {"status": "error", "message": "Internal server error"}


@app.get("/api/agents")
async def get_agents():
    """获取所有 Agent 状态"""
    return {
        "agents": {
            name: agent.to_dict()
            for name, agent in agents.items()
        }
    }


@app.get("/api/workflow")
async def get_workflow():
    """获取工作流信息（静态模板已废弃，现在由 CEO 动态生成 DAG）"""
    return {
        "status": "dynamic",
        "description": "工作流由 CEO Agent 根据需求动态生成，不再使用静态 YAML 定义",
        "template": "templates/project_template.yaml",
    }


@app.get("/api/devops/services")
async def get_devops_services(client_id: str = ""):
    """只返回指定客户的部署。"""
    if not client_id:
        return {"error": "client_id is required"}
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    services = devops.get_all_services(client_id)
    return {
        "total": len(services),
        "services": services,
    }


@app.get("/api/devops/deployments/{deployment_id}")
async def get_devops_service_detail(deployment_id: str, client_id: str = ""):
    """获取单个服务的详细信息"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    if not client_id:
        return {"error": "client_id is required"}
    detail = devops.get_service_detail(deployment_id, client_id)
    if not detail:
        return {"error": f"Deployment not found: {deployment_id}"}

    return detail


@app.post("/api/devops/deployments/{deployment_id}/restart")
async def restart_devops_service(deployment_id: str, client_id: str = ""):
    """重启一个部署。只接受 deployment_id。"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    if not client_id:
        return {"error": "client_id is required"}
    service = devops.get_deployment(deployment_id, client_id)
    if not service:
        return {"error": "Deployment not found"}
    success = await devops.docker.restart(service.container_name)
    if success and service.container_name.startswith("coagent-app-d"):
        try:
            await devops.refresh_runtime_route(service)
            service.mark_running()
            await devops._save_deployment(service, client_id)
            await devops.monitor.start_monitoring(service)
        except Exception:
            logger.exception("运行服务重启后恢复路由失败")
            return {"status": "error", "message": "Service restarted but route recovery failed"}

    if success:
        return {"status": "success", "message": f"Service {service.service_name} restarted"}
    else:
        return {"status": "error", "message": f"Failed to restart service {service.service_name}"}


@app.post("/api/devops/deployments/{deployment_id}/stop")
async def stop_devops_service(deployment_id: str, client_id: str = ""):
    """停止一个部署。只接受 deployment_id。"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    if not client_id:
        return {"error": "client_id is required"}
    service = devops.get_deployment(deployment_id, client_id)
    if not service:
        return {"error": "Deployment not found"}

    # 停止监控
    await devops.monitor.stop_monitoring(service.service_name)

    # 停止容器
    success = await devops.docker.stop(service.container_name)

    if success:
        service.mark_stopped()
        # 释放端口
        await devops.port_manager.release(service.port)
        service.mark_stopped()
        await devops._save_deployment(service)
        # 删除该部署的路由；运行服务使用客户前缀路径。
        if service.container_name.startswith("coagent-app-d"):
            from urllib.parse import urlsplit
            expected = f"c/{devops._sanitize_name(client_id)}/{deployment_id}"
            if urlsplit(service.url).path.strip("/") != expected:
                return {"status": "error", "message": "Invalid deployment route"}
            devops._remove_static_location(expected)
            if "成功" not in devops._reload_nginx():
                return {"status": "error", "message": "Nginx reload failed"}
        else:
            await devops.nginx.remove_service(deployment_id)
            await devops.nginx.reload()

        return {"status": "success", "message": f"Service {service.service_name} stopped"}
    else:
        return {"status": "error", "message": f"Failed to stop service {service.service_name}"}


@app.post("/api/feishu/webhook")
@limiter.limit("120/minute")
async def feishu_webhook(request: Request):
    """
    飞书事件回调端点

    处理：
    1. URL 验证（challenge）
    2. 消息事件（im.message.receive_v1）
    3. 卡片回调（action）

    P0 #6 修复：
      - 强制要求 FEISHU_APP_SECRET 配置（未配置 → 启动 panic）
      - 签名公式改为 app_secret + timestamp + encrypt_key + body
      - 签名缺失/不匹配 → 401，不放行
    """
    # Step 0: 验证签名
    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    signature = request.headers.get("X-Lark-Signature", "")

    body_bytes = await request.body()

    # P0 #6 修复：app_secret 是签名验证的强制要求
    if not settings.FEISHU_APP_SECRET:
        logger.error("[Security] FEISHU_APP_SECRET 未配置，拒绝所有飞书 webhook 请求")
        if audit_logger:
            await audit_logger.log_auth_failure(
                agent_id="system",
                action="feishu_webhook",
                reason="FEISHU_APP_SECRET not configured",
            )
        return JSONResponse(
            status_code=503,
            content={"error": "Service misconfigured: FEISHU_APP_SECRET required"},
        )

    if not signature or not timestamp:
        logger.warning("[Security] 飞书 webhook 缺少签名头")
        if audit_logger:
            await audit_logger.log_auth_failure(
                agent_id="system",
                action="feishu_webhook",
                reason="missing X-Lark-Signature or X-Lark-Request-Timestamp",
            )
        return JSONResponse(
            status_code=401,
            content={"error": "Missing signature headers"},
        )

    expected = FeishuClient.verify_webhook_signature(
        timestamp, body_bytes,
        app_secret=settings.FEISHU_APP_SECRET,
        encrypt_key=settings.FEISHU_ENCRYPT_KEY,
    )

    # 恒定时间比较，避免计时攻击
    if not hmac.compare_digest(expected, signature):
        logger.warning("[Security] 飞书签名验证失败")
        if audit_logger:
            await audit_logger.log_auth_failure(
                agent_id="system",
                action="feishu_webhook",
                reason="signature verification failed",
            )
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    body = json.loads(body_bytes.decode("utf-8"))
    logger.info(f"收到飞书回调: {json.dumps(body, ensure_ascii=False)[:200]}")

    # 1. URL 验证请求
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    # 2. 检查是否有 header（事件订阅格式）
    header = body.get("header", {})
    event_type = header.get("event_type", "")

    if event_type == "im.message.receive_v1":
        # 消息事件
        parsed = FeishuClient.parse_message_event(body)
        if parsed and parsed.get("type") == "message":
            chat_id = parsed["chat_id"]
            text = parsed["text"]
            message_id = parsed["message_id"]
            sender_id = parsed.get("sender_id", "")

            if not text:
                return {"code": 0}

            # P2 #25 修复：webhook 幂等性（飞书会重试）
            if state_manager and message_id:
                try:
                    is_first = await state_manager.dedup_check(
                        f"feishu_msg:{message_id}", ttl=86400
                    )
                    if not is_first:
                        logger.info(f"飞书重复消息，跳过: message_id={message_id}")
                        return {"code": 0}
                except Exception as e:
                    logger.warning(f"[P2 #25] 飞书去重检查失败: {e}")

            client_id = await get_or_create_client("feishu", chat_id)
            logger.info("feishu client_id=%s chat_id=%s", client_id, chat_id)
            ceo = agents.get("ceo")
            if ceo:
                asyncio.create_task(
                    ceo.handle_feishu_message(chat_id, text, message_id, sender_id, client_id=client_id)
                )
            else:
                logger.error("CEO Agent 未初始化")

    # 3. 卡片回调（飞书卡片按钮点击）
    if "action" in body:
        action_data = body.get("action", {})
        value = action_data.get("value", {})
        chat_id = body.get("open_chat_id", "")
        # P0 #5 修复：从 operator.open_id 提取点击者身份，用于横向越权校验
        operator = body.get("operator", {}) or {}
        sender_open_id = operator.get("open_id", "")
        if value and chat_id:
            ceo = agents.get("ceo")
            if ceo:
                asyncio.create_task(
                    ceo.handle_feishu_message(
                        chat_id=chat_id,
                        text=json.dumps(value),
                        message_id=body.get("open_message_id", ""),
                        sender_id=sender_open_id,
                    )
                )
        # 卡片回调返回空字典（不更新卡片）
        return {}

    return {"code": 0}


@app.get("/api/wecom/ceo/callback")
async def wecom_ceo_verify(request: Request):
    """
    企业微信 CEO 应用回调 URL 验证（GET 请求）

    企业微信配置回调 URL 时会发送 GET 请求验证
    """
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    echostr = request.query_params.get("echostr", "")

    try:
        echostr_decrypted = wecom_ceo_client.verify_callback(
            msg_signature, timestamp, nonce, echostr
        )
        return PlainTextResponse(echostr_decrypted)
    except Exception as e:
        logger.error(f"企业微信 CEO 回调验证失败: {e}", exc_info=True)
        return PlainTextResponse("Verification failed", status_code=403)


# CEO 消息去重集合
_ceo_processed_messages: set[str] = set()


@app.post("/api/wecom/ceo/callback")
@limiter.limit("120/minute")
async def wecom_ceo_callback(request: Request):
    """
    企业微信 CEO 智能机器人消息回调（POST 请求）

    处理老板发来的消息，交给 CEO Agent 处理
    """
    global _ceo_processed_messages

    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")

    body = await request.body()
    encrypt = WecomClient.parse_callback_message(body)

    if not encrypt:
        return {"error": "Invalid message"}

    try:
        msg = wecom_ceo_client.decrypt_message(msg_signature, timestamp, nonce, encrypt)
    except Exception as e:
        logger.error(f"企业微信 CEO 消息解密失败: {e}", exc_info=True)
        return {"error": "Decryption failed"}

    # 提取消息内容（支持 JSON 和 XML 格式）
    if isinstance(msg, dict):
        msg_type = msg.get("msgtype", "")
        if msg_type == "text":
            content = msg.get("text", {}).get("content", "").strip()
        elif msg_type == "event":
            # 忽略事件消息
            return {"code": 0}
        else:
            content = ""
        from_user = msg.get("from", {}).get("userid", "")
        msg_id = msg.get("msgid", "")
        response_url = msg.get("response_url", "")
    else:
        msg_type = msg.get("MsgType", "")
        content = msg.get("Content", "").strip()
        from_user = msg.get("FromUserName", "")
        msg_id = msg.get("MsgId", "")
        response_url = ""

    if not content:
        return {"error": "Empty message"}

    # 消息去重（P1 #15 修复：使用 Redis SETNX 替代内存 set）
    # 旧版内存 set 在并发不安全 + 重启丢失 + 多实例不一致
    # Redis SETNX 原子操作，跨进程生效，TTL 24h 自动清理
    if state_manager:
        try:
            is_first = await state_manager.dedup_check(f"wecom_ceo:{msg_id}", ttl=86400)
            if not is_first:
                logger.info(f"CEO 重复消息，跳过: msg_id={msg_id}")
                return {"code": 0}
        except Exception as e:
            logger.warning(f"Redis 去重检查失败，回退到内存 set: {e}")
            # 降级：使用内存 set（保留旧行为）
            if msg_id in _ceo_processed_messages:
                logger.info(f"CEO 重复消息（降级模式），跳过: msg_id={msg_id}")
                return {"code": 0}
            _ceo_processed_messages.add(msg_id)
            if len(_ceo_processed_messages) > 1000:
                _ceo_processed_messages = set(list(_ceo_processed_messages)[-500:])
    else:
        # state_manager 未初始化时的兼容路径
        if msg_id in _ceo_processed_messages:
            logger.info(f"CEO 重复消息（无 state_manager），跳过: msg_id={msg_id}")
            return {"code": 0}
        _ceo_processed_messages.add(msg_id)

    client_id = await get_or_create_client("wecom_ceo", from_user)
    logger.info("wecom ceo client_id=%s from=%s", client_id, from_user)

    # 交给 CEO Agent 处理
    ceo = agents.get("ceo")
    if ceo:
        try:
            # CEO Agent 处理消息并回复
            response = await ceo.handle_channel_message(client_id, content, msg_id)

            # 通过智能机器人的 response_url 回复
            if response_url and response:
                # P0 #4 修复：response_url 来自回调体，先做白名单校验（防 SSRF + 数据外泄）
                try:
                    WecomClient._validate_response_url(response_url)
                except ValueError as e:
                    logger.error(f"[SSRF] 拒绝可疑 response_url: {e}")
                    if audit_logger:
                        await audit_logger.log_auth_failure(
                            agent_id="ceo",
                            action="wecom_response_url_blocked",
                            reason=str(e),
                        )
                    return {"error": "Invalid response URL"}

                client = await wecom_ceo_client._get_client()
                resp = await client.post(
                    response_url,
                    json={
                        "msgtype": "markdown",
                        "markdown": {
                            "content": response,
                        },
                    },
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("errcode") != 0:
                    logger.error(f"CEO 回复失败: {result}")
                else:
                    logger.info(f"CEO 回复成功: {response[:50]}...")

            return {"code": 0}
        except Exception as e:
            logger.error(f"CEO 处理消息失败: {e}", exc_info=True)
            return {"error": "Message processing failed"}
    else:
        logger.error("CEO Agent 未初始化")
        return {"error": "CEO Agent not available"}


@app.get("/api/wecom/cs/callback")
async def wecom_cs_verify(request: Request):
    """
    企业微信客服应用回调 URL 验证（GET 请求）

    企业微信配置回调 URL 时会发送 GET 请求验证
    """
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    echostr = request.query_params.get("echostr", "")

    try:
        echostr_decrypted = wecom_cs_client.verify_callback(
            msg_signature, timestamp, nonce, echostr
        )
        return PlainTextResponse(echostr_decrypted)
    except Exception as e:
        logger.error(f"企业微信客服回调验证失败: {e}", exc_info=True)
        return PlainTextResponse("Verification failed", status_code=403)


@app.post("/api/wecom/cs/callback")
@limiter.limit("120/minute")
async def wecom_cs_callback(request: Request):
    """
    企业微信客服应用消息回调（POST 请求）

    处理外部联系人（客户）发来的消息：
    1. 解密消息
    2. 放入队列
    3. 如果需要排队，发送排队提示
    4. 立即返回（不阻塞企业微信回调）
    """
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")

    body = await request.body()
    logger.info(f"企业微信回调原始数据: {body[:500]}")

    encrypt = WecomClient.parse_callback_message(body)

    if not encrypt:
        # 尝试直接从请求体中提取
        try:
            body_str = body.decode("utf-8")
            if "<Encrypt>" in body_str:
                import re
                match = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>", body_str)
                if match:
                    encrypt = match.group(1)
        except Exception as e:
            logger.error(f"提取加密数据失败: {e}")

    if not encrypt:
        return {"error": "Invalid message"}

    try:
        msg = wecom_cs_client.decrypt_message(msg_signature, timestamp, nonce, encrypt)
    except Exception as e:
        logger.error(f"企业微信客服消息解密失败: {e}", exc_info=True)
        return {"error": "Decryption failed"}

    # 提取消息内容（支持 XML 和 JSON 两种格式）
    logger.info(f"解密后的消息: {msg}")

    # 智能机器人发来的是 JSON 格式
    if isinstance(msg, dict):
        msg_type = msg.get("msgtype", "")
        if msg_type == "text":
            content = msg.get("text", {}).get("content", "").strip()
        else:
            content = ""
        from_user = msg.get("from", {}).get("userid", "")
        msg_id = msg.get("msgid", "")
        response_url = msg.get("response_url", "")  # 智能机器人的回复 URL
    else:
        # 普通应用发来的是 XML 格式（已解析为 dict）
        msg_type = msg.get("MsgType", "")
        content = msg.get("Content", "").strip()
        from_user = msg.get("FromUserName", "")
        msg_id = msg.get("MsgId", "")
        response_url = ""

    if msg_type != "text":
        return {"error": "Unsupported message type"}

    if not content:
        return {"error": "Empty message"}

    logger.info(f"企业微信客服收到消息: from={from_user}, content={content[:50]}...")

    if not content:
        return {"error": "Empty message"}

    logger.info(f"企业微信客服收到消息: from={from_user}, content={content[:50]}...")

    # P2 #25 修复：webhook 幂等性（企业微信 5 秒超时重试）
    # 用 Redis SETNX 去重，避免重复入队
    if state_manager and msg_id:
        try:
            is_first = await state_manager.dedup_check(f"wecom_cs:{msg_id}", ttl=86400)
            if not is_first:
                logger.info(f"CS 重复消息，跳过入队: msg_id={msg_id}")
                return {"code": 0}
        except Exception as e:
            logger.warning(f"[P2 #25] CS 去重检查失败: {e}")

    client_id = await get_or_create_client("wecom_cs", from_user)
    logger.info("wecom cs client_id=%s from=%s", client_id, from_user)
    cs_queue = CSMessageQueue(state_manager.redis, client_id=client_id)
    queued_msg = QueuedMessage(
        customer_id=client_id,
        text=content,
        wecom_msg_id=msg_id,
        metadata={"response_url": response_url} if response_url else {},
    )
    queue_len = await cs_queue.enqueue(queued_msg)

    # 如果前面有人在等待，发送排队提示
    if queue_len > 0:
        try:
            notify_text = f"您前面有 {queue_len} 位客户在等待，预计 {queue_len * 30} 秒内回复您。"
            result = await wecom_cs_client.send_text_to_external(from_user, notify_text)
            # 保存提示消息 ID 用于后续撤回
            notify_msg_id = result.get("msgid", "")
            if notify_msg_id:
                await cs_queue.save_notify_msg_id(from_user, notify_msg_id)
        except Exception as e:
            logger.warning(f"发送排队提示失败: {e}")

    return {"code": 0}


# ========== 主入口 ==========

if __name__ == "__main__":
    ssl_kwargs = {}
    if settings.SSL_CERT_FILE and settings.SSL_KEY_FILE:
        ssl_kwargs = {
            "ssl_certfile": settings.SSL_CERT_FILE,
            "ssl_keyfile": settings.SSL_KEY_FILE,
        }
    elif not settings.HOST.startswith("127.") and not settings.HOST.startswith("localhost"):
        logger.warning("[Security] Running without TLS on non-localhost address")

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,  # 生产环境禁止热重载
        **ssl_kwargs,
    )
