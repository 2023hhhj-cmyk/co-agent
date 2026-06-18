"""WecomClient URL 白名单校验测试 — 覆盖 P0 #4 (SSRF + 数据外泄)"""

import pytest

from channels.wecom import WecomClient


class TestWecomClientResponseUrlValidation:
    """P0 #4: response_url 白名单 + IP 防护"""

    def setup_method(self):
        # 跳过 DNS 解析做单元测试，直接测 URL 解析逻辑
        pass

    def test_valid_qq_url_passes(self, monkeypatch):
        """合法 qq.com URL → 通过"""
        # Mock DNS 解析为公网 IP（测试环境可能 DNS 不可用或解析到 0.0.0.0）
        import socket
        monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
        url = "https://api.qq.com/callback"
        assert WecomClient._validate_response_url(url) == url

    def test_valid_tencent_subdomain_passes(self, monkeypatch):
        """tencent.com 子域 → 通过"""
        import socket
        monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
        url = "https://smartbot.tencent.com/api/reply"
        assert WecomClient._validate_response_url(url) == url

    def test_valid_weixin_qq_passes(self, monkeypatch):
        """weixin.qq.com → 通过"""
        import socket
        monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
        url = "https://qyapi.weixin.qq.com/cgi-bin/message/reply"
        assert WecomClient._validate_response_url(url) == url

    def test_http_scheme_rejected(self):
        """http:// 必须拒绝（必须 https）"""
        with pytest.raises(ValueError, match="https"):
            WecomClient._validate_response_url("http://api.qq.com/callback")

    def test_unknown_domain_rejected(self):
        """非白名单域名 → 拒绝（防数据外泄到 attacker.com）"""
        with pytest.raises(ValueError, match="白名单"):
            WecomClient._validate_response_url("https://evil.com/exfil")

    def test_subdomain_spoofing_rejected(self):
        """域名后缀欺骗：qq.com.evil.com → 拒绝"""
        with pytest.raises(ValueError, match="白名单"):
            WecomClient._validate_response_url("https://qq.com.evil.com/callback")

    def test_subdomain_match_allowed(self, monkeypatch):
        """子域匹配白名单父域 → 通过（api.qq.com 通过 qq.com 白名单）"""
        import socket
        monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
        assert WecomClient._validate_response_url("https://api.qq.com/x") == "https://api.qq.com/x"

    def test_empty_url_rejected(self):
        """空 URL → 拒绝"""
        with pytest.raises(ValueError, match="为空"):
            WecomClient._validate_response_url("")

    def test_none_url_rejected(self):
        """None → 拒绝"""
        with pytest.raises(ValueError, match="为空"):
            WecomClient._validate_response_url(None)  # type: ignore[arg-type]

    def test_ip_literal_rejected(self):
        """IP 字面量（防止数字 IP 绕过域名白名单）"""
        with pytest.raises(ValueError, match="IP"):
            WecomClient._validate_response_url("https://1.2.3.4/callback")

    def test_ipv6_literal_rejected(self):
        """IPv6 字面量 → 拒绝"""
        with pytest.raises(ValueError, match="IP"):
            WecomClient._validate_response_url("https://[::1]/callback")

    def test_malformed_url_rejected(self):
        """畸形 URL → 拒绝（scheme 校验先于 host 校验）"""
        with pytest.raises(ValueError, match="https"):
            WecomClient._validate_response_url("not a url at all")

    def test_url_with_port_in_whitelisted_host(self, monkeypatch):
        """白名单域名带端口 → 通过（端口不参与白名单匹配）"""
        import socket
        monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
        url = "https://api.qq.com:8080/path"
        assert WecomClient._validate_response_url(url) == url

    def test_url_with_query_string(self, monkeypatch):
        """带 query string → 通过"""
        import socket
        monkeypatch.setattr(socket, "gethostbyname", lambda h: "8.8.8.8")
        url = "https://api.qq.com/callback?token=abc&foo=bar"
        assert WecomClient._validate_response_url(url) == url


class TestWecomClientResponseUrlPrivateIPBlocking:
    """P0 #4 进阶：DNS 解析后内网/回环/链路本地 IP 拦截"""

    def test_loopback_ip_blocked(self, monkeypatch):
        """127.0.0.1 → 拒绝"""
        import socket
        monkeypatch.setattr(
            socket, "gethostbyname",
            lambda h: "127.0.0.1" if h == "api.qq.com" else socket.gethostbyname(h)
        )
        with pytest.raises(ValueError, match="内网|loopback|保留"):
            WecomClient._validate_response_url("https://api.qq.com/x")

    def test_private_ip_10_blocked(self, monkeypatch):
        """10.0.0.0/8 → 拒绝"""
        import socket
        monkeypatch.setattr(
            socket, "gethostbyname",
            lambda h: "10.0.0.1" if h == "api.qq.com" else socket.gethostbyname(h)
        )
        with pytest.raises(ValueError, match="内网"):
            WecomClient._validate_response_url("https://api.qq.com/x")

    def test_private_ip_192_blocked(self, monkeypatch):
        """192.168.0.0/16 → 拒绝"""
        import socket
        monkeypatch.setattr(
            socket, "gethostbyname",
            lambda h: "192.168.1.1" if h == "api.qq.com" else socket.gethostbyname(h)
        )
        with pytest.raises(ValueError, match="内网"):
            WecomClient._validate_response_url("https://api.qq.com/x")

    def test_aws_metadata_blocked(self, monkeypatch):
        """AWS 元数据 169.254.169.254 → 拒绝"""
        import socket
        monkeypatch.setattr(
            socket, "gethostbyname",
            lambda h: "169.254.169.254" if h == "api.qq.com" else socket.gethostbyname(h)
        )
        with pytest.raises(ValueError, match="内网|link"):
            WecomClient._validate_response_url("https://api.qq.com/x")
