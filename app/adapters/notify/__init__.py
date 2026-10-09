"""通知渠道适配层（FR-4.2）。

支持企业微信 / Server 酱 / Telegram / 邮件(SMTP) / Bark / 自定义 Webhook /
面板内通知中心，多渠道并行、每个可单独开关。

设计约束：

- **不得抛异常上抛业务层**（FR-1.3 精神）：渠道故障只记失败计数，
  巡检必须继续。坏 webhook 不能拖垮健康监测。
- **凭据只在发送瞬间解密**，不缓存明文。
- 全部走 async；邮件用 :mod:`aiosmtplib` 的替代方案——标准库 smtplib
  是同步的，故放入线程执行避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import smtplib
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 10.0


def _validate_http_url(url: str) -> str | None:
    """校验通知 URL scheme 为 http/https（防 SSRF）。

    PT 工具的 webhook 目标通常是用户自有服务，内网地址合法，故只校验
    scheme 而不强拒内网。返回错误原因或 None。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不允许的 URL scheme：{parsed.scheme or '(空)'}"
    if not parsed.hostname:
        return "URL 缺少主机名"
    return None


@dataclass(frozen=True, slots=True)
class AlertPayload:
    """待推送的告警内容。"""

    level: str
    category: str
    message: str
    title: str = ""
    #: 终端可点击的跳转链接（面板详情页）
    url: str = ""
    info_hash: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def emoji(self) -> str:
        return {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(self.level, "⚪")

    @property
    def display_title(self) -> str:
        return self.title or self.category

    def to_text(self) -> str:
        """通用文本表示。"""
        lines = [f"{self.emoji} [{self.level.upper()}] {self.display_title}"]
        if self.message:
            lines.append(self.message)
        for k, v in self.extra.items():
            lines.append(f"  {k}: {v}")
        if self.url:
            lines.append(self.url)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SendResult:
    """发送结果。"""

    ok: bool
    error: str = ""


class NotifyChannelAdapter(ABC):
    """渠道统一接口。"""

    channel_type: str = "unknown"

    def __init__(self, config: dict[str, object], secret: str | None = None) -> None:
        self._config = config
        self._secret = secret

    def cfg(self, key: str, default: str = "") -> str:
        val = self._config.get(key, default)
        return str(val) if val is not None else default

    @abstractmethod
    async def send(self, payload: AlertPayload) -> SendResult:
        """发送告警。**不得抛异常**。"""

    async def test(self) -> SendResult:
        """连通性自检（设置页"测试"按钮）。"""
        return await self.send(
            AlertPayload(
                level="info",
                category="test",
                title="SeedHelper 渠道自检",
                message="这是一条测试消息，收到即表示渠道配置正确。",
            )
        )


class WebhookAdapter(NotifyChannelAdapter):
    """通用 Webhook（自定义 JSON POST）。"""

    channel_type = "webhook"

    async def send(self, payload: AlertPayload) -> SendResult:
        url = self.cfg("url")
        if not url:
            return SendResult(False, "缺少 url 配置")
        if (err := _validate_http_url(url)):
            return SendResult(False, err)
        body = {
            "level": payload.level,
            "category": payload.category,
            "title": payload.display_title,
            "message": payload.message,
            "url": payload.url,
            "text": payload.to_text(),
        }
        headers = {"Content-Type": "application/json"}
        if self._secret:
            headers[self.cfg("secret_header", "Authorization")] = self._secret
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code >= 400:
                return SendResult(False, f"HTTP {resp.status_code}: {resp.text[:120]}")
            return SendResult(True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"{type(exc).__name__}: {exc}")


class WeComAdapter(NotifyChannelAdapter):
    """企业微信群机器人。"""

    channel_type = "wecom"

    async def send(self, payload: AlertPayload) -> SendResult:
        url = self._secret or self.cfg("webhook_url")
        if not url:
            return SendResult(False, "缺少 webhook（企业微信机器人地址）")
        if (err := _validate_http_url(url)):
            return SendResult(False, err)
        # 企业微信 markdown 上限 4096 字节，超长必须截断否则静默失败
        content = payload.to_text()[:3800]
        body = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(url, json=body)
            data = _safe_json(resp)
            if data.get("errcode") not in (0, None):
                return SendResult(False, f"errcode={data.get('errcode')} {data.get('errmsg')}")
            return SendResult(True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"{type(exc).__name__}: {exc}")


class ServerChanAdapter(NotifyChannelAdapter):
    """Server 酱（push key 作为 secret）。"""

    channel_type = "serverchan"

    async def send(self, payload: AlertPayload) -> SendResult:
        if not self._secret:
            return SendResult(False, "缺少 Server 酱 SendKey")
        title = payload.display_title[:32]
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(
                    f"https://sctapi.ftqq.com/{self._secret}.send",
                    data={"title": title, "desp": payload.to_text()},
                )
            data = _safe_json(resp)
            if data.get("code") not in (0, None):
                return SendResult(False, str(data.get("message") or data)[:150])
            return SendResult(True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"{type(exc).__name__}: {exc}")


class TelegramAdapter(NotifyChannelAdapter):
    """Telegram Bot（bot token 作为 secret）。"""

    channel_type = "telegram"

    async def send(self, payload: AlertPayload) -> SendResult:
        token = self._secret
        chat_id = self.cfg("chat_id")
        if not token or not chat_id:
            return SendResult(False, "缺少 bot token 或 chat_id")
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": payload.to_text(),
                        "disable_web_page_preview": False,
                    },
                )
            data = _safe_json(resp)
            if not data.get("ok"):
                return SendResult(False, str(data.get("description"))[:150])
            return SendResult(True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"{type(exc).__name__}: {exc}")


class BarkAdapter(NotifyChannelAdapter):
    """Bark iOS 推送。"""

    channel_type = "bark"

    async def send(self, payload: AlertPayload) -> SendResult:
        key = self._secret or self.cfg("device_key")
        host = self.cfg("host", "https://api.day.app")
        if not key:
            return SendResult(False, "缺少 device key")
        url = f"{host.rstrip('/')}/{key}"
        if (err := _validate_http_url(url)):
            return SendResult(False, err)
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={
                        "title": payload.display_title[:40],
                        "body": payload.message[:200] or payload.to_text()[:200],
                        "group": payload.category,
                        "level": "timeSensitive"
                        if payload.level == "critical"
                        else "active",
                    },
                )
            if resp.status_code >= 400:
                return SendResult(False, f"HTTP {resp.status_code}")
            return SendResult(True)
        except httpx.HTTPError as exc:
            return SendResult(False, f"{type(exc).__name__}: {exc}")


class EmailAdapter(NotifyChannelAdapter):
    """SMTP 邮件。密码作为 secret。

    标准库 ``smtplib`` 是同步的，故放入线程执行——**绝不能**在事件循环里
    直接调用，否则 SMTP 超时会阻塞整个巡检。
    """

    channel_type = "email"

    async def send(self, payload: AlertPayload) -> SendResult:
        host = self.cfg("host")
        port = int(self.cfg("port", "465"))
        sender = self.cfg("username") or self.cfg("from")
        to_addrs = [a.strip() for a in self.cfg("to", "").split(",") if a.strip()]
        if not host or not sender or not to_addrs:
            return SendResult(False, "缺少 host / username / to")
        if not self._secret:
            return SendResult(False, "缺少 SMTP 密码")

        msg = EmailMessage()
        msg["Subject"] = f"[SeedHelper] {payload.display_title}"
        msg["From"] = sender
        msg["To"] = ", ".join(to_addrs)
        msg.set_content(payload.to_text())

        use_ssl = self.cfg("ssl", "true").lower() == "true"
        try:
            await asyncio.to_thread(
                _smtp_send, host, port, sender, self._secret, to_addrs, msg, use_ssl
            )
            return SendResult(True)
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(False, f"{type(exc).__name__}: {exc}")

    async def test(self) -> SendResult:
        return await self.send(
            AlertPayload(
                level="info",
                category="test",
                title="SeedHelper 邮件自检",
                message="收到即表示 SMTP 配置正确。",
            )
        )


class PanelNotifier:
    """面板内通知中心（FR-4.2）。

    不发外部请求——告警已在 ``alerts`` 表落库，面板读表即可。
    保留适配器形态是为了让渠道列表统一处理。
    """

    channel_type = "panel"

    def __init__(self) -> None:
        self._pending: list[AlertPayload] = []

    async def send(self, payload: AlertPayload) -> SendResult:
        self._pending.append(payload)
        if len(self._pending) > 500:
            del self._pending[:-500]
        return SendResult(True)

    def drain(self) -> list[AlertPayload]:
        out = list(self._pending)
        self._pending.clear()
        return out


#: 渠道类型 → 适配器
CHANNEL_TYPES: dict[str, type[NotifyChannelAdapter]] = {
    "wecom": WeComAdapter,
    "serverchan": ServerChanAdapter,
    "telegram": TelegramAdapter,
    "email": EmailAdapter,
    "bark": BarkAdapter,
    "webhook": WebhookAdapter,
}


def build_adapter(
    channel_type: str, config: dict[str, object], secret: str | None
) -> NotifyChannelAdapter | PanelNotifier:
    """按类型构造渠道适配器。未知类型回退到 Webhook。"""
    cls = CHANNEL_TYPES.get(channel_type, WebhookAdapter)
    return cls(config, secret)


def _safe_json(resp: httpx.Response) -> dict[str, object]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _smtp_send(
    host: str,
    port: int,
    sender: str,
    password: str,
    to_addrs: list[str],
    msg: EmailMessage,
    use_ssl: bool,
) -> None:
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=DEFAULT_TIMEOUT) as s:
            s.login(sender, password)
            s.send_message(msg, from_addr=sender, to_addrs=to_addrs)
    else:
        with smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT) as s:
            s.starttls()
            s.login(sender, password)
            s.send_message(msg, from_addr=sender, to_addrs=to_addrs)