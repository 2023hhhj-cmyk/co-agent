"""
飞书通信层 - 封装飞书开放平台 API

功能：
- WebSocket 长连接接收消息（不需要公网回调地址）
- 发送文本/卡片消息
- 回复消息
- tenant_access_token 自动管理
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from base64 import b64encode
from typing import Any, Callable, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# 飞书 API 基础地址
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """飞书 API 客户端 - 支持 WebSocket 长连接"""

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
    ):
        self.app_id = app_id or settings.FEISHU_APP_ID
        self.app_secret = app_secret or settings.FEISHU_APP_SECRET
        self._client: Optional[httpx.AsyncClient] = None

        # token 缓存
        self._tenant_token: str = ""
        self._token_expire_at: float = 0

        # 长连接
        self._ws_process = None
        self._msg_queue = None
        self._message_handler: Optional[Callable] = None
        self._card_handler: Optional[Callable] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._ws_process and self._ws_process.is_alive():
            self._ws_process.terminate()

    # ========== 消息接收 ==========

    def set_message_handler(self, handler: Callable) -> None:
        """设置消息处理回调"""
        self._message_handler = handler

    def set_card_handler(self, handler: Callable) -> None:
        """设置卡片按钮回调处理"""
        self._card_handler = handler

    def start_ws(self, loop: asyncio.AbstractEventLoop = None) -> None:
        """
        启动飞书长连接（在后台线程中运行）

        lark.ws.Client 在独立线程中运行，消息通过 queue.Queue 传递到轮询线程，
        轮询线程通过 asyncio.run_coroutine_threadsafe 提交到主事件循环处理。
        """
        import queue
        import threading

        main_loop = loop or asyncio.get_event_loop()
        self._msg_queue = queue.Queue()
        message_handler = self._message_handler

        # lark SDK 消息回调（在 lark 线程中执行）
        def on_lark_message(parsed: dict):
            logger.info(f"飞书收到消息，放入队列: {parsed.get('text', '')[:50]}")
            self._msg_queue.put(parsed)

        # 轮询线程：从队列取消息，提交到主事件循环
        def _poll():
            logger.info("消息轮询线程已启动")
            while True:
                try:
                    parsed = self._msg_queue.get(timeout=5.0)
                    logger.info(f"轮询到消息: {parsed.get('text', '')[:50]}")
                    if message_handler:
                        asyncio.run_coroutine_threadsafe(
                            message_handler(parsed), main_loop
                        )
                        logger.info("消息已提交到事件循环处理")
                except Exception as e:
                    if "Empty" not in str(type(e).__name__):
                        logger.error(f"轮询异常: {e}")
                    continue

        # 启动轮询线程
        poll_thread = threading.Thread(target=_poll, daemon=True)
        poll_thread.start()

        # 启动 lark WebSocket 线程
        def _run_lark():
            self._start_lark_ws(on_lark_message)

        lark_thread = threading.Thread(target=_run_lark, daemon=True)
        lark_thread.start()

        logger.info("飞书长连接已启动（线程模式）")

    def _start_lark_ws(self, on_message_callback: Callable) -> None:
        """在当前线程中启动 lark WebSocket"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        def _on_message(data: P2ImMessageReceiveV1):
            try:
                event = data.event
                message = event.message
                sender = event.sender

                if message.message_type != "text":
                    logger.info(f"忽略非文本消息: type={message.message_type}")
                    return

                content_obj = json.loads(message.content)
                text = content_obj.get("text", "").strip()

                if text.startswith("@"):
                    parts = text.split(" ", 1)
                    text = parts[1] if len(parts) > 1 else ""

                sender_id = sender.sender_id.open_id if sender.sender_id else ""

                parsed = {
                    "type": "message",
                    "message_id": message.message_id,
                    "chat_id": message.chat_id,
                    "chat_type": message.chat_type,  # "p2p" or "group"
                    "text": text.strip(),
                    "sender_id": sender_id,
                    "create_time": message.create_time,
                }

                logger.info(f"lark 回调收到消息: {parsed['text'][:50]}")
                on_message_callback(parsed)

            except Exception as e:
                logger.error(f"处理消息失败: {e}", exc_info=True)

        # 卡片按钮回调处理
        def _on_card_action(data):
            try:
                # data.event 里有 action.value（按钮的 value）和 operator.open_id（点击者）
                event = data.event
                action = event.action
                value = action.value or {}
                operator_id = event.operator.open_id if event.operator else ""
                chat_id = ""

                # 从 context 中获取 chat_id
                if hasattr(event, 'context') and event.context:
                    chat_id = getattr(event.context, 'open_chat_id', '') or ''

                logger.info(f"卡片回调: action={value.get('action')}, operator={operator_id}")

                if self._card_handler:
                    self._card_handler({
                        "action": {"value": value},
                        "open_id": operator_id,
                        "open_chat_id": chat_id,
                    })

                # 返回 None 表示不更新卡片（lark SDK 会自动处理）
                return None
            except Exception as e:
                logger.error(f"处理卡片回调失败: {e}", exc_info=True)
                return None

        builder = lark.EventDispatcherHandler.builder(
            settings.FEISHU_VERIFICATION_TOKEN,
            settings.FEISHU_ENCRYPT_KEY,
        ).register_p2_im_message_receive_v1(_on_message)

        # 注册卡片回调处理器
        builder.register_p2_card_action_trigger(_on_card_action)

        event_handler = builder.build()

        ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        logger.info("lark WebSocket 客户端启动中...")
        ws_client.start()

    # ========== Token 管理 ==========

    async def _get_tenant_token(self) -> str:
        """
        获取 tenant_access_token

        飞书 token 有效期 2 小时，提前 5 分钟刷新
        """
        now = time.time()
        if self._tenant_token and now < self._token_expire_at - 300:
            return self._tenant_token

        client = await self._get_client()
        resp = await client.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"获取飞书 token 失败: {data}")

        self._tenant_token = data["tenant_access_token"]
        self._token_expire_at = now + data.get("expire", 7200)
        logger.info("飞书 tenant_access_token 已刷新")
        return self._tenant_token

    async def _auth_headers(self) -> dict[str, str]:
        """获取带认证的请求头"""
        token = await self._get_tenant_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ========== 发送消息 ==========

    async def send_text(self, chat_id: str, text: str) -> dict:
        """
        发送文本消息到会话

        Args:
            chat_id: 会话 ID（chat_id）
            text: 消息文本

        Returns:
            飞书 API 响应
        """
        return await self._send_message(
            receive_id=chat_id,
            msg_type="text",
            content=json.dumps({"text": text}),
            receive_id_type="chat_id",
        )

    async def send_to_user(self, open_id: str, text: str) -> dict:
        """
        发送文本消息给用户（通过 open_id）

        Args:
            open_id: 用户 open_id
            text: 消息文本

        Returns:
            飞书 API 响应
        """
        return await self._send_message(
            receive_id=open_id,
            msg_type="text",
            content=json.dumps({"text": text}),
            receive_id_type="open_id",
        )

    async def send_card(self, chat_id: str, card: dict) -> dict:
        """
        发送交互式卡片消息

        Args:
            chat_id: 会话 ID
            card: 卡片结构体

        Returns:
            飞书 API 响应
        """
        return await self._send_message(
            receive_id=chat_id,
            msg_type="interactive",
            content=json.dumps(card),
            receive_id_type="chat_id",
        )

    async def reply_text(self, message_id: str, text: str) -> dict:
        """
        回复文本消息

        Args:
            message_id: 要回复的消息 ID
            text: 回复文本

        Returns:
            飞书 API 响应
        """
        return await self._reply_message(
            message_id=message_id,
            msg_type="text",
            content=json.dumps({"text": text}),
        )

    async def reply_card(self, message_id: str, card: dict) -> dict:
        """
        回复卡片消息

        Args:
            message_id: 要回复的消息 ID
            card: 卡片结构体

        Returns:
            飞书 API 响应
        """
        return await self._reply_message(
            message_id=message_id,
            msg_type="interactive",
            content=json.dumps(card),
        )

    # ========== 内部方法 ==========

    async def _send_message(
        self,
        receive_id: str,
        msg_type: str,
        content: str,
        receive_id_type: str = "chat_id",
    ) -> dict:
        """发送消息的底层方法"""
        headers = await self._auth_headers()
        client = await self._get_client()

        resp = await client.post(
            f"{FEISHU_API_BASE}/im/v1/messages",
            headers=headers,
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content,
            },
        )
        if resp.status_code != 200:
            logger.error(f"飞书发送消息 HTTP {resp.status_code}: {resp.text}")
            resp.raise_for_status()

        data = resp.json()

        if data.get("code") != 0:
            logger.error(f"飞书发送消息失败: {data}")
            raise Exception(f"飞书发送消息失败: {data.get('msg', 'unknown error')}")

        logger.info(f"飞书消息已发送: receive_id={receive_id}, msg_type={msg_type}")
        return data

    async def _reply_message(
        self,
        message_id: str,
        msg_type: str,
        content: str,
    ) -> dict:
        """回复消息的底层方法"""
        headers = await self._auth_headers()
        client = await self._get_client()

        resp = await client.post(
            f"{FEISHU_API_BASE}/im/v1/messages/{message_id}/reply",
            headers=headers,
            json={
                "msg_type": msg_type,
                "content": content,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            logger.error(f"飞书回复消息失败: {data}")
            raise Exception(f"飞书回复消息失败: {data.get('msg', 'unknown error')}")

        logger.info(f"飞书消息已回复: message_id={message_id}")
        return data

    # ========== 工具方法 ==========

    @staticmethod
    def parse_message_event(body: dict) -> Optional[dict]:
        """
        解析飞书消息事件

        Args:
            body: Webhook 请求体

        Returns:
            解析后的消息信息，或 None（非消息事件）
        """
        # URL 验证请求
        if "challenge" in body:
            return {"type": "challenge", "challenge": body["challenge"]}

        # 消息事件
        event = body.get("event", {})
        if not event:
            return None

        message = event.get("message", {})
        if not message:
            return None

        # 只处理文本消息
        msg_type = message.get("message_type", "")
        if msg_type != "text":
            logger.info(f"忽略非文本消息: type={msg_type}")
            return None

        # 提取发送者信息
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {}).get("open_id", "")

        # 解析消息内容
        content = message.get("content", "{}")
        try:
            content_obj = json.loads(content)
            text = content_obj.get("text", "")
        except json.JSONDecodeError:
            text = content

        # 去掉 @机器人 的部分
        text = text.strip()
        if text.startswith("@"):
            # 移除 @mention 部分（格式为 @_user_xxx）
            parts = text.split(" ", 1)
            text = parts[1] if len(parts) > 1 else ""

        return {
            "type": "message",
            "message_id": message.get("message_id", ""),
            "chat_id": message.get("chat_id", ""),
            "chat_type": message.get("chat_type", ""),
            "text": text.strip(),
            "sender_id": sender_id,
            "create_time": message.get("create_time", ""),
        }

    @staticmethod
    def verify_webhook_signature(
        timestamp: str,
        body_bytes: bytes,
        app_secret: str,
        encrypt_key: str = "",
    ) -> str:
        """
        生成飞书 Webhook 签名字符串

        P0 #6 修复：原实现错误地把 verification_token 同时用作签名字段
        和 HMAC 密钥。飞书官方算法是：
            stringToSign = timestamp + "\\n" + encrypt_key + "\\n" + body
            signature   = base64(HMAC-SHA256(key=app_secret, msg=stringToSign))

        Args:
            timestamp: X-Lark-Request-Timestamp header
            body_bytes: 原始请求体 bytes（未解密时的 raw JSON）
            app_secret: 飞书应用的 app_secret（HMAC 密钥）
            encrypt_key: 飞书应用的 encrypt_key（stringToSign 第二段；未启用加密时传空串）

        Returns:
            签名 base64 字符串（与 X-Lark-Signature 比较）

        Raises:
            ValueError: app_secret 为空时
        """
        if not app_secret:
            raise ValueError("FEISHU_APP_SECRET 未配置，无法验证签名")

        # 关键：body 必须是原始字节（飞书发送的 raw body），不是解密后的明文
        # 这里我们只对 raw body 签名，调用方传入的就是 raw body
        try:
            body_str = body_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body_str = body_bytes.decode("utf-8", errors="replace")

        string_to_sign = f"{timestamp}\n{encrypt_key}\n{body_str}"
        computed = hmac.new(
            app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return b64encode(computed).decode("utf-8")
