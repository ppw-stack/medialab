"""巡检 → 告警 的编排（FR-4.3 策略在此真正生效）。

:class:`~app.services.inspector.Inspector` 只产出 :class:`Finding`，
本模块负责：落库 → 决策（去重/汇总/风暴/静默）→ 多渠道投递。

分离的理由：巡检必须能在**没有任何通知渠道**的情况下独立运行（用户
还没配 webhook），此时仍要有告警历史可供面板查看。
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.metrics import stage_duration
from app.db.models import AlertLevel, AlertPolicy
from app.db.session import session_scope
from app.services.alerter import Alerter, AlertPolicyConfig
from app.services.factory import AdapterFactory
from app.services.inspector import Inspector, InspectReport

logger = get_logger(__name__)

#: 巡检互斥锁：定时调度与手动触发不可并发（防重复告警投递）
_inspect_lock = asyncio.Lock()


async def load_policy() -> AlertPolicyConfig:
    """从 ``alert_policy`` 表读策略；无记录时用默认值（FR-4.3）。"""
    async with session_scope() as session:
        row = (
            await session.execute(select(AlertPolicy).where(AlertPolicy.id == 1))
        ).scalar_one_or_none()
        if row is None:
            return AlertPolicyConfig()
        return AlertPolicyConfig(
            mute_window_hours=row.mute_window_hours,
            warning_daily_digest=row.warning_daily_digest,
            critical_immediate=row.critical_immediate,
            storm_threshold=row.storm_threshold,
            quiet_hours=row.quiet_hours,
        )


async def inspect_and_alert(factory: AdapterFactory) -> InspectReport:
    """一轮巡检 + 告警投递。调度作业入口。"""
    async with _inspect_lock:  # ponytail: 全局锁，多租户时按 scope 分锁
        return await _inspect_and_alert_impl(factory)


async def _inspect_and_alert_impl(factory: AdapterFactory) -> InspectReport:
    started = time.monotonic()
    inspector = Inspector(factory)
    report = await inspector.run_once()

    if not report.findings:
        logger.info("本轮巡检无异常")
        return report

    policy = await load_policy()

    # 必须分两次事务：先 commit record，再去查 dedupe。
    # 否则同会话内读到的就是刚插入的那条，必然 self-suppress。
    async with session_scope() as session:
        alerter = Alerter(session, policy)
        for f in report.findings:
            await alerter.record(
                f.level,
                f.category,
                f.message,
                info_hash=f.info_hash,
                torrent_id=f.torrent_id,
            )

    # 第二个事务：dedupe 检查看到的是已 commit 的历史，不会与刚插入的混淆
    async with session_scope() as session:
        alerter = Alerter(session, policy)
        candidates = [
            (f.level, f.category, f.info_hash)
            for f in report.findings
            if f.level != AlertLevel.INFO
        ]
        plan = await alerter.plan(candidates)

        # 投递（也用同一会话：channel 状态、send_metric 同步）
        sent = 0
        for payload in plan.alerts:
            results = await alerter.dispatch(payload)
            if any(results.values()):
                sent += 1

    logger.info(
        "告警完成：发现 %d，去重压制 %d，风暴=%s，静默=%s，实际投递 %d 条",
        len(report.findings),
        plan.suppressed,
        plan.storm,
        plan.quiet,
        sent,
    )
    stage_duration.labels(stage="inspect").observe(time.monotonic() - started)
    return report
