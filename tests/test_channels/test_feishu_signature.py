"""Feishu Webhook 签名验证测试 — 覆盖 P0 #6"""

import base64
import hashlib
import hmac
import inspect
import json

import pytest

from channels.feishu import FeishuClient


# ========== P0 #6 修复测试 ==========

class TestFeishuWebhookSignature:
    """P0 #6: 签名公式必须用 app_secret 作为 HMAC 密钥"""

    def test_missing_app_secret_raises(self):
        """app_secret 为空 → 抛 ValueError（防止配置缺失导致绕过）"""
        with pytest.raises(ValueError, match="FEISHU_APP_SECRET"):
            FeishuClient.verify_webhook_signature(
                timestamp="1700000000",
                body_bytes=b'{"event":"test"}',
                app_secret="",
            )

    def test_signature_uses_app_secret_not_verification_token(self):
        """源码中必须使用 app_secret 作为 HMAC 密钥

        修复前：把 verification_token 同时用作 stringToSign 字段和 HMAC 密钥
        修复后：app_secret 作为 HMAC 密钥，encrypt_key 作为 stringToSign 第二段
        """
        sig = inspect.signature(FeishuClient.verify_webhook_signature)
        params = list(sig.parameters.keys())
        assert "app_secret" in params, "verify_webhook_signature 必须接收 app_secret 参数"
        assert "encrypt_key" in params, "verify_webhook_signature 必须接收 encrypt_key 参数"

        # 检查 HMAC key 来自 app_secret 参数
        source = inspect.getsource(FeishuClient.verify_webhook_signature)
        assert "app_secret.encode" in source, "HMAC key 必须用 app_secret.encode(...)"
        # 不应再把 verification_token 当作 HMAC 密钥
        assert "hmac.new(verification_token" not in source, (
            "P0 #6 修复：HMAC 密钥不能再用 verification_token"
        )

    def test_signature_with_known_vector(self):
        """已知向量签名一致性测试"""
        timestamp = "1700000000"
        body = b'{"event":"test","type":"message"}'
        app_secret = "test_app_secret_abc123"
        encrypt_key = "test_encrypt_key_xyz"

        # 手算预期签名
        string_to_sign = f"{timestamp}\n{encrypt_key}\n{body.decode('utf-8')}"
        expected = base64.b64encode(
            hmac.new(
                app_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        actual = FeishuClient.verify_webhook_signature(
            timestamp=timestamp,
            body_bytes=body,
            app_secret=app_secret,
            encrypt_key=encrypt_key,
        )

        assert actual == expected

    def test_signature_with_empty_encrypt_key(self):
        """未启用加密时（encrypt_key 为空）应正常工作

        注意：stringToSign 结构始终是 timestamp + \n + encrypt_key + \n + body
        即使 encrypt_key 为空，也保留两个 \n（这是飞书官方规范）
        """
        timestamp = "1700000000"
        body = b'{"challenge":"abc"}'
        app_secret = "secret_no_encryption"

        # 即使 encrypt_key 为空，分隔符 \n 仍存在
        string_to_sign = f"{timestamp}\n\n{body.decode('utf-8')}"
        expected = base64.b64encode(
            hmac.new(
                app_secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        actual = FeishuClient.verify_webhook_signature(
            timestamp=timestamp,
            body_bytes=body,
            app_secret=app_secret,
            encrypt_key="",  # 未启用加密
        )

        assert actual == expected

    def test_signature_with_unicode_body(self):
        """body 含 Unicode（中文消息）应正确处理"""
        timestamp = "1700000000"
        body = '{"text":"你好世界"}'.encode("utf-8")
        app_secret = "secret_unicode_test"

        actual = FeishuClient.verify_webhook_signature(
            timestamp=timestamp,
            body_bytes=body,
            app_secret=app_secret,
        )

        # 不抛异常，能算出签名
        assert len(actual) > 0
        # 验证可被 base64 解码
        decoded = base64.b64decode(actual)
        assert len(decoded) == 32  # SHA-256 输出 32 字节

    def test_signature_with_invalid_utf8_body(self):
        """body 含非法 UTF-8 字节应优雅降级（errors='replace'）"""
        timestamp = "1700000000"
        body = b'\xff\xfe{"event":"test"}'  # 含 BOM-like 字节
        app_secret = "secret"

        # 不应抛 UnicodeDecodeError
        sig = FeishuClient.verify_webhook_signature(
            timestamp=timestamp,
            body_bytes=body,
            app_secret=app_secret,
        )
        assert len(sig) > 0

    def test_different_secrets_produce_different_signatures(self):
        """不同 app_secret 产生不同签名（防止密钥混淆）"""
        body = b'{"x":1}'
        sig_a = FeishuClient.verify_webhook_signature(
            timestamp="1700000000", body_bytes=body, app_secret="secret_A"
        )
        sig_b = FeishuClient.verify_webhook_signature(
            timestamp="1700000000", body_bytes=body, app_secret="secret_B"
        )
        assert sig_a != sig_b

    def test_tampered_body_changes_signature(self):
        """篡改 body 应改变签名"""
        body1 = b'{"action":"confirm"}'
        body2 = b'{"action":"execute_arbitrary_command"}'

        sig1 = FeishuClient.verify_webhook_signature(
            timestamp="1700000000", body_bytes=body1, app_secret="s"
        )
        sig2 = FeishuClient.verify_webhook_signature(
            timestamp="1700000000", body_bytes=body2, app_secret="s"
        )

        assert sig1 != sig2
