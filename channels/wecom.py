"""
企业微信通信层 - 封装企业微信开放平台 API

功能：
- Token 管理（access_token 自动刷新）
- 回调验证（URL 验证 + 消息解密）
- 发送消息（文本消息给外部联系人/内部员工）
- 撤回消息
- 获取外部联系人信息

企业微信两个应用：
- CEO 应用：内部员工（老板）沟通
- 客服应用：外部联系人（客户）沟通
"""

import asyncio
import hashlib
import json
import logging
import struct
import time
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode
from typing import Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

# 企业微信 API 基础地址
WECOM_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WecomCrypto:
    """企业微信消息加解密"""

    def __init__(self, token: str, aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        # AES 密钥 = Base64Decode(EncodingAESKey + "=")
        self.aes_key = b64decode(aes_key + "=")
        self.iv = self.aes_key[:16]

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        """验证消息签名"""
        params = sorted([self.token, timestamp, nonce, encrypt])
        sha1 = hashlib.sha1("".join(params).encode("utf-8")).hexdigest()
        return sha1 == msg_signature

    def decrypt(self, encrypt: str) -> str:
        """解密消息"""
        from Crypto.Cipher import AES
        try:
            # 处理可能的转义字符
            encrypt = encrypt.replace("\\/", "/")

            cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
            plain_text = cipher.decrypt(b64decode(encrypt))
            # 去除 PKCS7 填充
            pad_len = plain_text[-1]
            plain_text = plain_text[:-pad_len]
            # 结构：random(16) + msg_len(4) + msg + corp_id
            content = plain_text[16:]
            msg_len = struct.unpack(">I", content[:4])[0]
            msg = content[4:4 + msg_len].decode("utf-8")
            logger.info(f"解密成功: {msg[:200]}")
            return msg
        except Exception as e:
            logger.error(f"解密失败: {e}, encrypt_length={len(encrypt)}")
            raise

    def encrypt(self, reply_msg: str) -> str:
        """加密回复消息"""
        from Crypto.Cipher import AES
        # 随机 16 字节 + 消息长度 + 消息 + corp_id
        import os
        random_bytes = os.urandom(16)
        msg_bytes = reply_msg.encode("utf-8")
        msg_len = struct.pack(">I", len(msg_bytes))
        content = random_bytes + msg_len + msg_bytes + self.corp_id.encode("utf-8")
        # PKCS7 填充
        block_size = 32
        pad_len = block_size - (len(content) % block_size)
        content += bytes([pad_len]) * pad_len
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.iv)
        encrypted = cipher.encrypt(content)
        return b64encode(encrypted).decode("utf-8")

    def generate_signature(self, encrypt: str, timestamp: str, nonce: str) -> str:
        """生成回复消息的签名"""
        params = sorted([self.token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(params).encode("utf-8")).hexdigest()

    def generate_verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """验证回调 URL（返回解密后的 echostr）"""
        if not self.verify_signature(msg_signature, timestamp, nonce, echostr):
            raise ValueError("签名验证失败")
        return self.decrypt(echostr)


class WecomClient:
    """企业微信 API 客户端"""

    def __init__(
        self,
        corp_id: str = "",
        agent_id: str = "",
        secret: str = "",
        token: str = "",
        aes_key: str = "",
    ):
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.token = token
        self.aes_key = aes_key

        # token 缓存
        self._access_token: str = ""
        self._token_expire_at: float = 0

        # HTTP 客户端
        self._client: Optional[httpx.AsyncClient] = None

        # 加解密工具
        self.crypto = WecomCrypto(token, aes_key, corp_id) if token and aes_key else None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ========== Token 管理 ==========

    async def _get_access_token(self) -> str:
        """
        获取 access_token

        企业微信 token 有效期 2 小时，提前 5 分钟刷新
        """
        now = time.time()
        if self._access_token and now < self._token_expire_at - 300:
            return self._access_token

        client = await self._get_client()
        resp = await client.get(
            f"{WECOM_API_BASE}/token",
            params={
                "corpid": self.corp_id,
                "corpsecret": self.secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode") != 0:
            raise Exception(f"获取企业微信 token 失败: {data}")

        self._access_token = data["access_token"]
        self._token_expire_at = now + data.get("expires_in", 7200)
        logger.info("企业微信 access_token 已刷新")
        return self._access_token

    async def _auth_headers(self) -> dict[str, str]:
        """获取带认证的请求头"""
        token = await self._get_access_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _auth_params(self) -> dict[str, str]:
        """获取带认证的查询参数"""
        token = await self._get_access_token()
        return {"access_token": token}

    # ========== 回调处理 ==========

    def verify_callback(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """
        验证回调 URL

        企业微信配置回调 URL 时会发送验证请求，需要返回解密后的 echostr

        Returns:
            解密后的 echostr
        """
        if not self.crypto:
            raise ValueError("加解密工具未初始化（token 或 aes_key 为空）")
        return self.crypto.generate_verify_url(msg_signature, timestamp, nonce, echostr)

    def decrypt_message(self, msg_signature: str, timestamp: str, nonce: str, encrypt: str) -> dict:
        """
        解密企业微信推送的消息

        Args:
            msg_signature: 签名
            timestamp: 时间戳
            nonce: 随机数
            encrypt: 加密的消息体

        Returns:
            解析后的消息字典
        """
        if not self.crypto:
            raise ValueError("加解密工具未初始化")

        # 验证签名
        if not self.crypto.verify_signature(msg_signature, timestamp, nonce, encrypt):
            raise ValueError("消息签名验证失败")

        # 解密
        decrypted = self.crypto.decrypt(encrypt)
        logger.info(f"解密成功: {decrypted[:200]}")

        # 尝试解析为 JSON（智能机器人格式）
        try:
            return json.loads(decrypted)
        except json.JSONDecodeError:
            pass

        # 尝试解析为 XML（普通应用格式）
        try:
            root = ET.fromstring(decrypted)
            msg = {}
            for child in root:
                msg[child.tag] = child.text
            return msg
        except ET.ParseError:
            pass

        # 都不是，返回原始内容
        return {"raw": decrypted}

    def encrypt_reply(self, reply_xml: str, timestamp: str, nonce: str) -> str:
        """
        加密回复消息

        Args:
            reply_xml: 回复消息的 XML 字符串
            timestamp: 时间戳
            nonce: 随机数

        Returns:
            加密后的 XML 字符串
        """
        if not self.crypto:
            raise ValueError("加解密工具未初始化")

        encrypt = self.crypto.encrypt(reply_xml)
        signature = self.crypto.generate_signature(encrypt, timestamp, nonce)

        return f"""<xml>
<Encrypt><![CDATA[{encrypt}]]></Encrypt>
<MsgSignature><![CDATA[{signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""

    # ========== 发送消息 ==========

    async def send_text_to_external(self, external_userid: str, text: str, response_url: str = "") -> dict:
        """
        发送文本消息给外部联系人（客户）

        智能机器人使用 response_url 回复，普通应用使用客服消息接口

        Args:
            external_userid: 外部联系人 ID
            text: 消息文本
            response_url: 智能机器人的回复 URL（如果有）

        Returns:
            API 响应
        """
        client = await self._get_client()

        # 智能机器人使用 response_url 回复
        if response_url:
            resp = await client.post(
                response_url,
                json={
                    "msgtype": "text",
                    "text": {
                        "content": text,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("errcode") != 0:
                logger.error(f"智能机器人回复失败: {data}")
                raise Exception(f"智能机器人回复失败: {data.get('errmsg', 'unknown')}")

            logger.info(f"智能机器人回复已发送: response_url={response_url[:50]}...")
            return data

        # 普通应用使用客服消息接口
        params = await self._auth_params()
        resp = await client.post(
            f"{WECOM_API_BASE}/externalcontact/message/send",
            params=params,
            json={
                "chat_type": "single",
                "external_userid": [external_userid],
                "sender": "",
                "text": {
                    "content": text,
                },
                "msgtype": "text",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode") != 0:
            logger.error(f"企业微信发送外部消息失败: {data}")
            raise Exception(f"企业微信发送外部消息失败: {data.get('errmsg', 'unknown')}")

        logger.info(f"企业微信外部消息已发送: external_userid={external_userid}")
        return data

    async def send_text_to_user(self, userid: str, text: str) -> dict:
        """
        发送文本消息给内部员工

        Args:
            userid: 员工 ID
            text: 消息文本

        Returns:
            API 响应
        """
        params = await self._auth_params()
        client = await self._get_client()

        resp = await client.post(
            f"{WECOM_API_BASE}/message/send",
            params=params,
            json={
                "touser": userid,
                "msgtype": "text",
                "agentid": self.agent_id,
                "text": {
                    "content": text,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode") != 0:
            logger.error(f"企业微信发送内部消息失败: {data}")
            raise Exception(f"企业微信发送内部消息失败: {data.get('errmsg', 'unknown')}")

        logger.info(f"企业微信内部消息已发送: userid={userid}")
        return data

    async def send_text(self, userid: str, text: str, is_external: bool = False) -> dict:
        """
        发送文本消息（自动判断内外部）

        Args:
            userid: 用户 ID
            text: 消息文本
            is_external: 是否为外部联系人

        Returns:
            API 响应
        """
        if is_external:
            return await self.send_text_to_external(userid, text)
        else:
            return await self.send_text_to_user(userid, text)

    # ========== 撤回消息 ==========

    async def recall(self, msgid: str) -> dict:
        """
        撤回消息

        只能撤回 24 小时内的消息

        Args:
            msgid: 消息 ID

        Returns:
            API 响应
        """
        params = await self._auth_params()
        client = await self._get_client()

        resp = await client.post(
            f"{WECOM_API_BASE}/message/recall",
            params=params,
            json={
                "msgid": msgid,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode") != 0:
            logger.error(f"企业微信撤回消息失败: {data}")
            raise Exception(f"企业微信撤回消息失败: {data.get('errmsg', 'unknown')}")

        logger.info(f"企业微信消息已撤回: msgid={msgid}")
        return data

    # ========== 获取外部联系人信息 ==========

    async def get_external_contact(self, external_userid: str) -> dict:
        """
        获取外部联系人详情

        Args:
            external_userid: 外部联系人 ID

        Returns:
            外部联系人信息
        """
        params = await self._auth_params()
        client = await self._get_client()

        resp = await client.get(
            f"{WECOM_API_BASE}/externalcontact/get",
            params={**params, "external_userid": external_userid},
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("errcode") != 0:
            logger.error(f"获取外部联系人信息失败: {data}")
            raise Exception(f"获取外部联系人信息失败: {data.get('errmsg', 'unknown')}")

        return data

    # ========== 工具方法 ==========

    @staticmethod
    def parse_callback_message(body: bytes) -> Optional[str]:
        """
        从回调请求体中提取加密消息

        支持两种格式：
        1. XML 格式：<xml><Encrypt>...</Encrypt></xml>
        2. JSON 格式：{"encrypt": "..."}

        Args:
            body: 原始请求体

        Returns:
            加密的消息体，或 None
        """
        try:
            body_str = body.decode("utf-8").strip()

            # 尝试 JSON 格式（智能机器人使用）
            if body_str.startswith("{"):
                data = json.loads(body_str)
                return data.get("encrypt", "")

            # 尝试 XML 格式（普通应用使用）
            root = ET.fromstring(body)
            encrypt = root.findtext("Encrypt")
            return encrypt

        except Exception as e:
            logger.error(f"解析回调消息失败: {e}")
            return None

    @staticmethod
    def build_reply_text_xml(content: str) -> str:
        """构建文本回复消息的 XML"""
        return f"""<xml>
<Content><![CDATA[{content}]]></Content>
</xml>"""
