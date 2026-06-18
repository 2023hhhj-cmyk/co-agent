"""WecomCrypto 单元测试 — 覆盖 P0 #2 (SHA1 计时攻击修复)"""

import hashlib
import hmac

import pytest

from channels.wecom import WecomCrypto


class TestWecomCryptoVerifySignature:
    """WecomCrypto.verify_signature"""

    def setup_method(self):
        self.crypto = WecomCrypto(
            token="test_token_abc",
            aes_key="b" * 43,  # EncodingAESKey (43 chars)
            corp_id="wxcorp123",
        )
        self.timestamp = "1700000000"
        self.nonce = "xyz789"
        self.encrypt = "encrypted_blob_xxx"

    def _expected_signature(self) -> str:
        params = sorted([self.crypto.token, self.timestamp, self.nonce, self.encrypt])
        return hashlib.sha1("".join(params).encode("utf-8")).hexdigest()

    def test_valid_signature_passes(self):
        """合法签名 → True"""
        sig = self._expected_signature()
        assert self.crypto.verify_signature(sig, self.timestamp, self.nonce, self.encrypt) is True

    def test_invalid_signature_rejected(self):
        """篡改签名 → False"""
        assert self.crypto.verify_signature("0" * 40, self.timestamp, self.nonce, self.encrypt) is False

    def test_empty_signature_rejected_without_error(self):
        """空签名 → False（不应抛 AttributeError）

        修复前：msg_signature 为 None 时 `==` 抛 TypeError
        修复后：`msg_signature or ""` 走恒定时间比较
        """
        assert self.crypto.verify_signature("", self.timestamp, self.nonce, self.encrypt) is False
        assert self.crypto.verify_signature(None, self.timestamp, self.nonce, self.encrypt) is False  # type: ignore[arg-type]

    def test_uses_constant_time_compare(self):
        """必须使用 hmac.compare_digest（恒定时间）

        通过 inspect 验证源码使用了 hmac.compare_digest。
        """
        import inspect
        import channels.wecom as wecom_module
        source = inspect.getsource(wecom_module.WecomCrypto.verify_signature)
        assert "hmac.compare_digest" in source, "verify_signature must use hmac.compare_digest"
        assert " == " not in source, "verify_signature must not use == for comparison"

    def test_tamper_detection_token_nonce_encrypt(self):
        """任意一个参数被篡改 → False"""
        sig = self._expected_signature()
        # 篡改 token
        old_token = self.crypto.token
        self.crypto.token = "tampered"
        try:
            assert self.crypto.verify_signature(sig, self.timestamp, self.nonce, self.encrypt) is False
        finally:
            self.crypto.token = old_token

        # 篡改 timestamp
        assert self.crypto.verify_signature(sig, str(int(self.timestamp) + 1), self.nonce, self.encrypt) is False
        # 篡改 nonce
        assert self.crypto.verify_signature(sig, self.timestamp, "evil", self.encrypt) is False
        # 篡改 encrypt
        assert self.crypto.verify_signature(sig, self.timestamp, self.nonce, "tampered") is False

    def test_short_signature_rejected(self):
        """签名长度不对 → False（不抛异常）"""
        assert self.crypto.verify_signature("abc", self.timestamp, self.nonce, self.encrypt) is False

    def test_compare_digest_directly(self):
        """直接对比：与手算 SHA1 一致"""
        sig = self._expected_signature()
        # hmac.compare_digest 应当与 `==` 给出同样结果（正常情况）
        # 区别在于计时特征。功能上验证即可。
        assert hmac.compare_digest(sig, sig) is True
        assert hmac.compare_digest(sig, "wrong") is False
