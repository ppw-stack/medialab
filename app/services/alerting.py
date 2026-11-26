"""运维降级告警通知（面向 ``/readyz`` 的 ``degradations``）。

单实例运行下，启动降级不会触发容器重启；通过可选的 ``ALERT_WEBHOOK_URL``
在启动后异步推送一条汇总告警（同时 ``/metrics`` 暴露
``seedhelper_startup_degradations`` 供 Prometheus Alertmanager 抓取）。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Webhook 请求超时（秒）——告警绝不能拖慢启动或探针
_WEBHOOK_TIMEOUT_SEC = 5.0


def _build_payload(
    degradations: dict[str, dict[str, str]],
    *,
    app_name: str,
    env: str,
) -> dict[str, Any]:
    lines = [
        f"[{app_name}/{env}] /readyz 存在 {len(degradations)} 项启动降级："
    ]
    for comp, info in degradations.items():
        lines.append(f"- {comp}: {info.get('reason', '')} ({info.get('at', '')})")
    text = "\n".join(lines)
    return {
        "event": "readyz_degraded",
        "app": app_name,
        "env": env,
        "count": len(degradations),
        "degradations": degradations,
        # 兼容常见通用/群机器人 Webhook 的纯文本字段
        "text": text,
    }


async def notify_startup_degradations(
    degradations: dict[str, dict[str, str]],
    *,
    webhook_url: str | None = None,
) -> bool:
    """将启动降级明细推送到 Webhook；未配置或无降级时直接返回 False。"""
    if not degradations:
        return False
    settings = get_settings()
    url = (
        settings.alert_webhook_url if webhook_url is None else webhook_url
    ).strip()
    if not url:
        return False

    payload = _build_payload(
        degradations, app_name=settings.app_name, env=settings.env
    )
    try:
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "启动降级告警 Webhook 返回 HTTP %d：%s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
    except httpx.HTTPError as exc:
        logger.warning("启动降级告警 Webhook 发送失败：%s", exc)
        return False
    logger.info("已发送启动降级告警（%d 项）", len(degradations))
    return True
