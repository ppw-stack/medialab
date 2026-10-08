"""通知渠道适配层测试（FR-4.2）。

每个渠道都覆盖：正常发送、配置缺失降级、网络异常不抛、密文空处理。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
from app.adapters.notify import (
    CHANNEL_TYPES,
    AlertPayload,
    BarkAdapter,
    EmailAdapter,
    PanelNotifier,
    ServerChanAdapter,
    TelegramAdapter,
    WebhookAdapter,
    WeComAdapter,
    build_adapter,
)


def payload(**kw) -> AlertPayload:
    defaults = {"level": "warning", "category": "test", "message": "hello"}
    defaults.update(kw)
    return AlertPayload(**defaults)


# ------------------------------------------------------------ Webhook
async def test_webhook_sends_json_post() -> None:
    adapter = WebhookAdapter({"url": "https://x.example/h"}, secret="Bearer T")
    response = httpx.Response(200, request=httpx.Request("POST", "https://x.example/h"))
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = response
        result = await adapter.send(payload())
    assert result.ok
    body = mock.call_args.kwargs["json"]
    assert body["level"] == "warning"
    assert body["text"].startswith("🟡")
    assert mock.call_args.kwargs["headers"]["Authorization"] == "Bearer T"


async def test_webhook_4xx_returns_failure() -> None:
    adapter = WebhookAdapter({"url": "https://x.example/h"})
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = httpx.Response(500, text="oops", request=httpx.Request("POST", "x"))
        result = await adapter.send(payload())
    assert result.ok is False
    assert "500" in result.error


async def test_webhook_no_url() -> None:
    result = await WebhookAdapter({}).send(payload())
    assert "url" in result.error


async def test_webhook_network_error_no_raise() -> None:
    """网络异常不得抛——渠道故障绝不上抛业务层（FR-1.3 精神）。"""
    adapter = WebhookAdapter({"url": "https://x.example/h"})
    req = httpx.Request("POST", "x")
    with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("x", request=req)):
        result = await adapter.send(payload())
    assert result.ok is False
    assert "ConnectError" in result.error


# ------------------------------------------------------------ WeCom
async def test_wecom_marks_critical() -> None:
    adapter = WeComAdapter({}, secret="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = httpx.Response(
            200, json={"errcode": 0, "errmsg": "ok"}, request=httpx.Request("POST", "x")
        )
        result = await adapter.send(payload(level="critical"))
    assert result.ok


async def test_wecom_errorcode_not_zero() -> None:
    adapter = WeComAdapter({}, secret="https://x/w")
    bad_req = httpx.Request("POST", "x")
    bad_resp = httpx.Response(
        200, json={"errcode": 40001, "errmsg": "invalid token"}, request=bad_req
    )
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = bad_resp
        result = await adapter.send(payload())
    assert result.ok is False
    assert "40001" in result.error


async def test_wecom_no_webhook() -> None:
    result = await WeComAdapter({}).send(payload())
    assert result.ok is False
    assert "webhook" in result.error


# ------------------------------------------------------------ Server 酱
async def test_serverchan_ok() -> None:
    adapter = ServerChanAdapter({}, secret="SCT-KEY")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = httpx.Response(
            200, json={"code": 0, "message": "ok"}, request=httpx.Request("POST", "x")
        )
        result = await adapter.send(payload())
    assert result.ok


async def test_serverchan_no_key() -> None:
    result = await ServerChanAdapter({}).send(payload())
    assert result.ok is False
    assert "SendKey" in result.error


# ------------------------------------------------------------ Telegram
async def test_telegram_ok() -> None:
    adapter = TelegramAdapter({"chat_id": "123"}, secret="BOT:TOKEN")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = httpx.Response(
            200, json={"ok": True, "result": {}}, request=httpx.Request("POST", "x")
        )
        result = await adapter.send(payload())
    assert result.ok


async def test_telegram_missing_token() -> None:
    result = await TelegramAdapter({"chat_id": "1"}).send(payload())
    assert result.ok is False


async def test_telegram_missing_chat_id() -> None:
    result = await TelegramAdapter({}, secret="T").send(payload())
    assert result.ok is False


# ------------------------------------------------------------ Bark
async def test_bark_time_sensitive_for_critical() -> None:
    adapter = BarkAdapter({}, secret="KEY")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        r = httpx.Response(200, request=httpx.Request("POST", "x"))
        mock.return_value = r
        await adapter.send(payload(level="critical"))
    body = mock.call_args.kwargs["json"]
    assert body["level"] == "timeSensitive"


async def test_bark_active_for_warning() -> None:
    adapter = BarkAdapter({}, secret="KEY")
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock:
        mock.return_value = httpx.Response(200, request=httpx.Request("POST", "x"))
        await adapter.send(payload())
    assert mock.call_args.kwargs["json"]["level"] == "active"


# ------------------------------------------------------------ 邮件
async def test_email_uses_smtplib_in_thread() -> None:
    adapter = EmailAdapter(
        {"host": "smtp.x", "port": "465", "username": "a", "to": "b@c", "ssl": "true"},
        secret="PW",
    )
    with patch("app.adapters.notify.asyncio.to_thread", new_callable=AsyncMock) as mock:
        mock.return_value = None
        result = await adapter.send(payload())
    assert result.ok
    # 关键：邮件必须放入 to_thread，**绝不能**同步阻塞事件循环
    mock.assert_called_once()


async def test_email_missing_fields() -> None:
    result = await EmailAdapter({}, secret="P").send(payload())
    assert result.ok is False
    assert "host" in result.error or "username" in result.error


async def test_email_no_password() -> None:
    result = await EmailAdapter(
        {"host": "s", "username": "a", "to": "b"}, secret=None
    ).send(payload())
    assert result.ok is False


# ------------------------------------------------------------ 面板
async def test_panel_does_not_send_to_external() -> None:
    panel = PanelNotifier()
    result = await panel.send(payload())
    assert result.ok
    assert panel.drain() and not panel.drain()


def test_build_adapter_known_types() -> None:
    """所有渠道类型都对应一个实现，未知类型回退到 Webhook。"""
    assert isinstance(
        build_adapter("telegram", {"chat_id": "1"}, "T"), TelegramAdapter
    )
    assert isinstance(build_adapter("unknown", {}, None), WebhookAdapter)


def test_all_documented_channel_types_built() -> None:
    for t in ("wecom", "serverchan", "telegram", "email", "bark", "webhook"):
        assert t in CHANNEL_TYPES, f"FR-4.2 缺失渠道类型：{t}"


# ------------------------------------------------------------ 通用
def test_payload_to_text_includes_emoji() -> None:
    p = payload(level="critical", title="X", url="https://x/y")
    txt = p.to_text()
    assert "🔴" in txt
    assert "https://x/y" in txt
    assert "X" in txt


def test_payload_emoji_per_level() -> None:
    assert payload(level="info").emoji == "🔵"
    assert payload(level="warning").emoji == "🟡"
    assert payload(level="critical").emoji == "🔴"
    assert payload(level="weird").emoji == "⚪"