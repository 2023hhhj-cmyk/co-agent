"""
webhook_health.py — 飞书/企业微信回调 URL 健康检查

逻辑：
- 每小时调一次 3 个回调 URL（GET echostr 校验）
- 失败时：
  1. 写 /var/log/co-agent/webhook_health.log
  2. 发飞书消息到 webhook_alert chat（如果配了）

为什么根因 1 是"后台 URL 配错"必须用户查——我只是健康检查 + 告警。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# 检测的回调 URL（3 个）
WECOM_CEO_URL = os.environ.get("WECOM_CEO_CALLBACK_TEST_URL", "https://loveboos-ai.com/api/wecom/ceo/callback")
WECOM_CS_URL = os.environ.get("WECOM_CS_CALLBACK_TEST_URL", "https://loveboos-ai.com/api/wecom/cs/callback")
FEISHU_URL = os.environ.get("FEISHU_WEBHOOK_TEST_URL", "https://loveboos-ai.com/api/feishu/webhook")
LOVEBOOS_REDIRECT_URL = "https://loveboos-ai.com/"  # 用于测错配的重定向

CHECK_INTERVAL_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 10
LOG_DIR = "/var/log/co-agent"

# 飞书告警 chat（环境变量配）
FEISHU_ALERT_CHAT = os.environ.get("FEISHU_ALERT_CHAT_ID", "")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")


def _log(msg: str) -> None:
    """ponytail: 写文件日志 + stdout。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{datetime.now().isoformat()}] {msg}"
    try:
        with open(f"{LOG_DIR}/webhook_health.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    logger.info(line)


def _check_wecom(url: str, name: str) -> tuple[bool, str]:
    """测试企业微信 callback URL：GET 时返 echostr= 解密后明文。"""
    # ponytail: 用正确的企业微信签名算法（sorted([token, ts, nonce, echo]) SHA1）
    # 从 settings 读对应 token
    from config.settings import settings
    if "ceo" in name:
        token = getattr(settings, "WECOM_CEO_TOKEN", "") or ""
    else:
        token = getattr(settings, "WECOM_CS_TOKEN", "") or ""
    if not token:
        return False, "skip（环境变量缺 WECOM_*_TOKEN，无法构造签名）"

    ts = str(int(time.time()))
    nonce = f"m_{int(time.time()*1000)%100000}"
    echo = "monitor_echo_" + str(int(time.time()))

    # 企业微信签名：sorted([token, ts, nonce, echo]) SHA1
    params_sorted = sorted([token, ts, nonce, echo])
    msg_signature = hashlib.sha1("".join(params_sorted).encode("utf-8")).hexdigest()

    qs = {
        "msg_signature": msg_signature,
        "timestamp": ts,
        "nonce": nonce,
        "echostr": echo,
    }
    # ponytail: 502/503/504 是上游启动中，重试 1 次（避免误报）
    last_msg = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
                r = client.get(url, params=qs, follow_redirects=False)
            if r.status_code in (502, 503, 504) and attempt == 0:
                import time as _time
                _time.sleep(3)
                continue
            if r.status_code == 200:
                return True, f"ok (200) — 验证 URL 后端可达"
            if r.status_code in (301, 302, 307, 308):
                return False, f"redirect to {r.headers.get('Location', '?')}"
            if r.status_code == 404:
                return False, "404 — 回调 URL 路径错配（应在企业微信后台改成 " + url + "）"
            if r.status_code == 403:
                return False, "403 — 签名失败（token 与企微后台不一致）"
            if r.status_code == 500:
                return False, f"500 后端错（FastAPI exception）: {r.text[:100]}"
            return False, f"HTTP {r.status_code}: {r.text[:100]}"
        except Exception as e:
            last_msg = f"exception: {type(e).__name__}: {e}"
            if attempt == 0:
                continue
            return False, last_msg
    return False, last_msg or "502 重试后仍失败"


def _check_feishu(url: str) -> tuple[bool, str]:
    """测试飞书 webhook：发个空 challenge 看响应。"""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            # 飞书 URL 验证发 POST 带 challenge token
            r = client.post(
                url,
                json={
                    "challenge": "monitor_challenge",
                    "type": "url_verification",
                },
                follow_redirects=False,
            )
        if r.status_code == 200 and "challenge" in r.text:
            return True, "ok"
        if r.status_code in (301, 302):
            return False, f"redirect to {r.headers.get('Location', '?')}"
        if r.status_code == 404:
            return False, "404 — webhook URL 路径错配"
        return False, f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return False, f"exception: {type(e).__name__}: {e}"


async def _send_feishu_alert(message: str) -> None:
    """ponytail: 告警发飞书（如果配了 FEISHU_ALERT_CHAT + APP 凭证）。"""
    if not FEISHU_ALERT_CHAT or not FEISHU_APP_ID:
        logger.debug("飞书告警未配置 env，跳过")
        return
    try:
        from channels.feishu import FeishuClient
        client = FeishuClient()
        await client.send_text(FEISHU_ALERT_CHAT, message)
    except Exception as e:
        logger.warning(f"飞书告警发送失败: {e}")


async def run_webhook_health_check() -> dict:
    """ponytail: 跑一次完整健康检查 + 返结果（也可被 APScheduler 等调用）。"""
    results = {}
    for name, url in [
        ("wecom_ceo", WECOM_CEO_URL),
        ("wecom_cs", WECOM_CS_URL),
        ("feishu_webhook", FEISHU_URL),
    ]:
        if name.startswith("feishu"):
            ok, msg = _check_feishu(url)
        else:
            ok, msg = _check_wecom(url, name)
        results[name] = {"url": url, "ok": ok, "msg": msg}
        status = "✓" if ok else "✗"
        _log(f"{status} {name} ({url}): {msg}")

        if not ok:
            # ponytail: 同时发飞书告警（如果配了）
            alert = f"[Webhook 健康检查异常]\n{name} 失败\nURL: {url}\n原因: {msg}\n建议：去企业微信/飞书后台检查回调 URL 是否配对。预期：{url}"
            asyncio.create_task(_send_feishu_alert(alert))

    return results


async def webhooks_monitor_loop() -> None:
    """ponytail: 启动后先等 30s 让 FastAPI 热，然后跑一次，之后每 CHECK_INTERVAL_SECONDS 秒跑一次。"""
    _log(f"Webhook monitor started, interval={CHECK_INTERVAL_SECONDS}s, warmup=30s")
    await asyncio.sleep(30)  # ponytail: 等 FastAPI 启动（避免启动时 502 误报）
    while True:
        try:
            await run_webhook_health_check()
        except Exception as e:
            _log(f"monitor loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


def start_webhook_monitor() -> Optional[asyncio.Task]:
    """ponytail: 在 FastAPI lifespan 里调这个，启动后台 monitor 任务。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("当前无 running loop，跳过 webhook monitor 启动")
        return None
    return loop.create_task(webhooks_monitor_loop())
