"""
Muzhu-AI 多Agent协同系统 - 主程序入口

融合架构：
- MetaGPT 角色机制（Think-Act-Observe）
- ChatDev 图工作流（DAG 拓扑排序）
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from agents import (
    CEOAgent,
    CustomerServiceAgent,
    DeveloperAgent,
    DevOpsAgent,
    FinanceAgent,
    FrontendExpertAgent,
)
from channels.feishu import FeishuClient
from channels.wecom import WecomClient
from communication.message_bus import MessageBus
from communication.queue import CSMessageQueue, QueueWorker, QueuedMessage
from config.settings import settings
from models.message import Message
from storage.database import Database
from storage.state_manager import StateManager
from tools.llm.router import LLMRouter
from workflows.executor import WorkflowExecutor
from workflows.graph import WorkflowGraph
from workflows.loader import WorkflowLoader

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ========== 全局组件 ==========

# 消息总线
message_bus = MessageBus()

# LLM 路由器
llm_router = LLMRouter()

# 存储
state_manager = StateManager()
database = Database()

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
    """创建所有 Agent"""
    global agents

    # 先创建下级 Agent
    devops_agent = DevOpsAgent(
        llm_client=llm_router.get_client_by_agent("devops"),
        message_bus=message_bus,
    )

    agents = {
        "developer": DeveloperAgent(
            llm_client=llm_router.get_client_by_agent("developer"),
            message_bus=message_bus,
        ),
        "frontend_expert": FrontendExpertAgent(
            llm_client=llm_router.get_client_by_agent("frontend_expert"),
            message_bus=message_bus,
        ),
        "devops": devops_agent,
        "finance": FinanceAgent(
            llm_client=llm_router.get_client_by_agent("finance"),
            message_bus=message_bus,
        ),
    }

    # CEO 最后创建，注入飞书客户端和团队
    agents["ceo"] = CEOAgent(
        llm_client=llm_router.get_client_by_agent("ceo"),
        message_bus=message_bus,
        feishu_client=feishu_client,
        team=agents,
    )

    # 设置 DevOps Agent 的飞书客户端（用于告警通知）
    # 注意：需要 CEO Agent 的 chat_id，这里先设置客户端，chat_id 在首次消息时获取
    devops_agent._feishu_client = feishu_client

    return agents


def load_workflow():
    """加载工作流定义"""
    workflow_path = Path(__file__).parent / "workflows" / "yaml" / "requirement_flow.yaml"
    if workflow_path.exists():
        loader = WorkflowLoader()
        workflow_def = loader.load(workflow_path)
        graph = WorkflowGraph(workflow_def.graph)
        logger.info(f"Workflow loaded: {graph.id}")
        return graph
    else:
        logger.warning(f"Workflow file not found: {workflow_path}")
        return None


# ========== 生命周期管理 ==========

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    logger.info("Starting Muzhu-AI Agent System...")

    # 连接存储
    await state_manager.connect()
    await database.connect()

    # 创建 Agent
    create_agents()
    logger.info(f"Agents created: {list(agents.keys())}")

    # 加载工作流
    graph = load_workflow()
    if graph:
        app.state.workflow_graph = graph
        app.state.workflow_executor = WorkflowExecutor(graph, agents)
        logger.info("Workflow engine initialized")

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
    feishu_client.start_ws(loop=asyncio.get_event_loop())
    logger.info("飞书 WebSocket 长连接已启动")

    # 初始化客服系统
    cs_queue = CSMessageQueue(state_manager.redis)
    cs_agent = CustomerServiceAgent(
        llm_client=llm_router.get_client_by_agent("customer_service"),
        message_bus=message_bus,
        wecom_client=wecom_cs_client,
        db=database,
        redis_client=state_manager.redis,
        cs_queue=cs_queue,
        feishu_client=feishu_client,
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    """健康检查"""
    return {"status": "healthy"}


@app.post("/api/requirement")
async def create_requirement(requirement: dict):
    """
    创建需求 - 通过工作流执行

    流程：CEO → PM → [Dev + Design] → Deploy → Finance
    """
    content = requirement.get("content", "")
    if not content:
        return {"error": "Content is required"}

    # 创建输入消息
    input_message = Message(
        content=content,
        cause_by="user_requirement",
        sent_from="user",
        send_to={"*"},
    )

    # 检查工作流引擎是否可用
    if hasattr(app.state, "workflow_executor"):
        # 通过工作流执行
        logger.info(f"Executing workflow for requirement: {content[:50]}...")
        try:
            result = await app.state.workflow_executor.execute(input_message)
            return {
                "status": "success",
                "workflow_id": app.state.workflow_graph.id,
                "result": result,
            }
        except Exception as e:
            logger.error(f"Workflow execution failed: {e}")
            return {"status": "error", "message": str(e)}
    else:
        # 回退：直接调用 CEO
        logger.info("Workflow not available, falling back to CEO")
        ceo = agents.get("ceo")
        if ceo:
            result = await ceo.run(input_message)
            return {
                "status": "success",
                "result": result.to_dict() if result else None,
            }
        else:
            return {"error": "CEO agent not available"}


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
    """获取工作流信息"""
    if hasattr(app.state, "workflow_graph"):
        graph = app.state.workflow_graph
        return {
            "id": graph.id,
            "description": graph.description,
            "nodes": list(graph.nodes.keys()),
            "layers": graph.topological_sort(),
        }
    return {"error": "Workflow not loaded"}


@app.get("/api/messages/history")
async def get_message_history(limit: int = 100):
    """获取消息历史"""
    history = message_bus.get_history(limit)
    return {"messages": [msg.to_dict() for msg in history]}


@app.get("/api/devops/services")
async def get_devops_services():
    """获取所有已部署的服务"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    services = devops.get_all_services()
    return {
        "total": len(services),
        "services": services,
    }


@app.get("/api/devops/services/{service_name}")
async def get_devops_service_detail(service_name: str):
    """获取单个服务的详细信息"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    detail = devops.get_service_detail(service_name)
    if not detail:
        return {"error": f"Service not found: {service_name}"}

    return detail


@app.post("/api/devops/services/{service_name}/restart")
async def restart_devops_service(service_name: str):
    """重启服务"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    if service_name not in devops.deployed_services:
        return {"error": f"Service not found: {service_name}"}

    service = devops.deployed_services[service_name]
    success = await devops.docker.restart(service.container_name)

    if success:
        return {"status": "success", "message": f"Service {service_name} restarted"}
    else:
        return {"status": "error", "message": f"Failed to restart service {service_name}"}


@app.post("/api/devops/services/{service_name}/stop")
async def stop_devops_service(service_name: str):
    """停止服务"""
    devops = agents.get("devops")
    if not devops:
        return {"error": "DevOps agent not available"}

    if service_name not in devops.deployed_services:
        return {"error": f"Service not found: {service_name}"}

    service = devops.deployed_services[service_name]

    # 停止监控
    await devops.monitor.stop_monitoring(service_name)

    # 停止容器
    success = await devops.docker.stop(service.container_name)

    if success:
        service.mark_stopped()
        # 释放端口
        devops.port_manager.release(service.port)
        # 删除 Nginx 配置
        await devops.nginx.remove_service(service_name)
        await devops.nginx.reload()

        return {"status": "success", "message": f"Service {service_name} stopped"}
    else:
        return {"status": "error", "message": f"Failed to stop service {service_name}"}


@app.post("/api/feishu/webhook")
async def feishu_webhook(request: Request):
    """
    飞书事件回调端点

    处理：
    1. URL 验证（challenge）
    2. 消息事件（im.message.receive_v1）
    3. 卡片回调（action）
    """
    body = await request.json()
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

            # 异步交给 CEO 处理
            ceo = agents.get("ceo")
            if ceo:
                asyncio.create_task(
                    ceo.handle_feishu_message(chat_id, text, message_id, sender_id)
                )
            else:
                logger.error("CEO Agent 未初始化")

    # 3. 卡片回调（飞书卡片按钮点击）
    if "action" in body:
        action_data = body.get("action", {})
        value = action_data.get("value", {})
        chat_id = body.get("open_chat_id", "")
        if value and chat_id:
            ceo = agents.get("ceo")
            if ceo:
                asyncio.create_task(
                    ceo.handle_feishu_message(
                        chat_id=chat_id,
                        text=json.dumps(value),
                        message_id=body.get("open_message_id", ""),
                    )
                )

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
        logger.error(f"企业微信 CEO 回调验证失败: {e}")
        return PlainTextResponse(str(e), status_code=403)


# CEO 消息去重集合
_ceo_processed_messages: set[str] = set()


@app.post("/api/wecom/ceo/callback")
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
        logger.error(f"企业微信 CEO 消息解密失败: {e}")
        return {"error": str(e)}

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

    # 消息去重
    if msg_id in _ceo_processed_messages:
        logger.info(f"CEO 重复消息，跳过: msg_id={msg_id}")
        return {"code": 0}
    _ceo_processed_messages.add(msg_id)
    # 限制去重集合大小
    if len(_ceo_processed_messages) > 1000:
        _ceo_processed_messages = set(list(_ceo_processed_messages)[-500:])

    logger.info(f"企业微信 CEO 收到消息: from={from_user}, content={content[:50]}...")

    # 交给 CEO Agent 处理
    ceo = agents.get("ceo")
    if ceo:
        try:
            # CEO Agent 处理消息并回复
            response = await ceo._get_general_answer(content)

            # 通过智能机器人的 response_url 回复
            if response_url and response:
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
            logger.error(f"CEO 处理消息失败: {e}")
            return {"error": str(e)}
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
        logger.error(f"企业微信客服回调验证失败: {e}")
        return PlainTextResponse(str(e), status_code=403)


@app.post("/api/wecom/cs/callback")
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
        logger.error(f"企业微信客服消息解密失败: {e}")
        return {"error": str(e)}

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

    # 放入队列
    cs_queue = CSMessageQueue(state_manager.redis)
    queued_msg = QueuedMessage(
        customer_id=from_user,
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
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
