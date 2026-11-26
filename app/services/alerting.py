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
    """组装 Webhook 请求体（结构化字段 + 通用机器人的纯文本 ``text``）。

    :param degradations: 组件名 → ``{reason, at}`` 的降级明细。
    :param app_name: 应用名（``AppSettings.app_name``）。
    :param env: 运行环境（``AppSettings.env``）。
    :returns: 可直接 ``json=`` 提交的字典。
    """
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
    """将启动降级明细推送到 Webhook；未配置或无降级时直接返回 False。

    :param degradations: :func:`app.services.startup_status.snapshot` 的结果。
    :param webhook_url: 覆盖配置里的 ``ALERT_WEBHOOK_URL``（测试用）。
    :returns: True = 已成功推送；False = 未配置 / 无降级 / 推送失败（不抛异常）。
    """
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
    except Exception as exc:  # 告警是旁路：绝不能让异常逃逸成无人回收的 task
        # 不只 ``httpx.HTTPError``：``ALERT_WEBHOOK_URL`` 配成无 scheme 的
        # 地址时 httpx 抛的是 ``InvalidURL``（不属于 HTTPError），旧实现会
        # 让 fire-and-forget task 出现 "Task exception was never retrieved"，
        # 告警本身也静默丢失。告警链路必须是「尽力而为、绝不外抛」。
        logger.warning(
            "启动降级告警 Webhook 发送失败（%s）：%s", type(exc).__name__, exc
        )
        return False
    logger.info("已发送启动降级告警（%d 项）", len(degradations))
    return True
