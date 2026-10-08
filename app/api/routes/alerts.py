"""告警 API（FR-6.1 监控告警 + FR-4.2 渠道配置）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.adapters.notify import SendResult
from app.api.auth import require_session
from app.core.config import get_settings
from app.core.crypto import build_cipher
from app.core.timeutils import utcnow
from app.db.models import Alert, AlertPolicy, NotifyChannel
from app.db.session import session_scope
from app.services.alerter import Alerter
from app.services.factory import init_factory
from app.services.inspect_job import inspect_and_alert, load_policy

router = APIRouter(prefix="/alerts", dependencies=[Depends(require_session)], tags=["alerts"])


class AlertOut(BaseModel):
    id: int
    ts: datetime
    level: str
    category: str
    info_hash: str | None
    message: str
    dedupe_key: str
    acknowledged: bool
    muted_until: datetime | None


class ChannelIn(BaseModel):
    name: str
    type: str
    enabled: bool = False
    config: dict[str, object] = Field(default_factory=dict)
    secret: str | None = None
    min_level: str = "warning"


class ChannelOut(BaseModel):
    id: int
    name: str
    type: str
    enabled: bool
    min_level: str
    config: dict[str, object]
    has_secret: bool
    fail_count: int
    breaker_until: datetime | None
    last_ok_at: datetime | None
    last_error: str | None


class PolicyOut(BaseModel):
    mute_window_hours: int
    warning_daily_digest: bool
    critical_immediate: bool
    storm_threshold: int
    quiet_hours: str | None


class PolicyIn(BaseModel):
    mute_window_hours: int | None = Field(default=None, ge=0, le=168)
    warning_daily_digest: bool | None = None
    critical_immediate: bool | None = None
    storm_threshold: int | None = Field(default=None, ge=1, le=10000)
    quiet_hours: str | None = None


# ---------------------------------------------------------------- 告警列表
@router.get("", summary="告警列表（多维筛选 + 服务端分页）")
async def list_alerts(
    level: str | None = None,
    category: str | None = None,
    unacked_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    async with session_scope() as session:
        stmt = select(Alert)
        if level:
            stmt = stmt.where(Alert.level == level)
        if category:
            stmt = stmt.where(Alert.category == category)
        if unacked_only:
            stmt = stmt.where(Alert.acknowledged.is_(False))

        total = (
            await session.execute(
                select(func.count(Alert.id)).select_from(stmt.subquery())
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    stmt.order_by(Alert.ts.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            AlertOut(
                id=r.id,
                ts=r.ts,
                level=r.level,
                category=r.category,
                info_hash=r.info_hash,
                message=r.message[:500],
                dedupe_key=r.dedupe_key,
                acknowledged=r.acknowledged,
                muted_until=r.muted_until,
            )
            for r in rows
        ],
    }


@router.get("/stats", summary="今日告警统计（趋势面板）")
async def alert_stats() -> dict[str, Any]:
    async with session_scope() as session:
        alerter = Alerter(session, await load_policy())
        by_level = await alerter.today_stats()
        unacked = (
            await session.execute(
                select(func.count(Alert.id)).where(Alert.acknowledged.is_(False))
            )
        ).scalar_one()
        by_cat = (
            (
                await session.execute(
                    select(Alert.category, func.count(Alert.id))
                    .group_by(Alert.category)
                    .order_by(func.count(Alert.id).desc())
                    .limit(20)
                )
            )
            .all()
        )
    return {
        "today_by_level": by_level,
        "unacknowledged": unacked,
        "by_category": dict(by_cat),
    }


@router.post("/{alert_id}/ack", summary="确认告警")
async def acknowledge(alert_id: int) -> dict[str, int | bool]:
    async with session_scope() as session:
        row = (
            await session.execute(select(Alert).where(Alert.id == alert_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        row.acknowledged = True
        return {"id": alert_id, "acknowledged": True}


@router.post("/ack-all", summary="一键确认全部告警")
async def acknowledge_all() -> dict[str, int]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(Alert).where(Alert.acknowledged.is_(False))))
            .scalars()
            .all()
        )
        for row in rows:
            row.acknowledged = True
        return {"acknowledged": len(rows)}


@router.post("/{alert_id}/unmute", summary="解除该告警的静默窗口")
async def unmute(alert_id: int) -> dict[str, object]:
    """人工解除静默（FR-4.3 允许用户覆盖去重）。"""
    async with session_scope() as session:
        row = (
            await session.execute(select(Alert).where(Alert.id == alert_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        row.muted_until = utcnow()
        row.acknowledged = True
        return {"id": alert_id, "muted_until": row.muted_until}


@router.post("/inspect", summary="手动触发一轮巡检")
async def trigger_inspect() -> dict[str, Any]:
    """立即执行一轮巡检 + 告警（FR-6.1 监控告警面板）。"""
    factory = init_factory(build_cipher(get_settings()))
    report = await inspect_and_alert(factory)
    return {
        "inspected": report.inspected,
        "findings": len(report.findings),
        "critical": report.critical_count,
        "warning": report.warning_count,
        "by_category": report.by_category(),
        "downloaders": report.downloaders,
        "duration_ms": report.duration_ms,
    }


# ---------------------------------------------------------------- 策略
@router.get("/policy", summary="告警策略（FR-4.3）")
async def get_policy() -> PolicyOut:
    p = await load_policy()
    return PolicyOut(
        mute_window_hours=p.mute_window_hours,
        warning_daily_digest=p.warning_daily_digest,
        critical_immediate=p.critical_immediate,
        storm_threshold=p.storm_threshold,
        quiet_hours=p.quiet_hours,
    )


@router.put("/policy", summary="修改告警策略")
async def update_policy(payload: PolicyIn) -> PolicyOut:
    async with session_scope() as session:
        row = (
            await session.execute(select(AlertPolicy).where(AlertPolicy.id == 1))
        ).scalar_one_or_none()
        if row is None:
            row = AlertPolicy(id=1)
            session.add(row)
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(row, key, value)
    p = await load_policy()
    return PolicyOut(
        mute_window_hours=p.mute_window_hours,
        warning_daily_digest=p.warning_daily_digest,
        critical_immediate=p.critical_immediate,
        storm_threshold=p.storm_threshold,
        quiet_hours=p.quiet_hours,
    )


# ---------------------------------------------------------------- 渠道
@router.get("/channels", summary="通知渠道列表")
async def list_channels() -> list[ChannelOut]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(NotifyChannel).order_by(NotifyChannel.name)))
            .scalars()
            .all()
        )
        return [_channel_out(r) for r in rows]


@router.post("/channels", status_code=201, summary="新增通知渠道")
async def create_channel(payload: ChannelIn) -> ChannelOut:
    cipher = build_cipher(get_settings())
    async with session_scope() as session:
        exists = (
            await session.execute(
                select(NotifyChannel.id).where(NotifyChannel.name == payload.name)
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="渠道名已存在")
        ch = NotifyChannel(
            name=payload.name,
            type=payload.type,
            enabled=payload.enabled,
            config=payload.config,
            min_level=payload.min_level,
            key_version=get_settings().key_version,
        )
        if payload.secret:
            ch.secret_enc = cipher.encrypt(
                payload.secret, owner_type="notify_channel", owner_id=payload.name
            )
        session.add(ch)
        await session.flush()
        return _channel_out(ch)


@router.put("/channels/{channel_id}", summary="修改通知渠道")
async def update_channel(channel_id: int, payload: ChannelIn) -> ChannelOut:
    cipher = build_cipher(get_settings())
    async with session_scope() as session:
        ch = (
            await session.execute(
                select(NotifyChannel).where(NotifyChannel.id == channel_id)
            )
        ).scalar_one_or_none()
        if ch is None:
            raise HTTPException(status_code=404, detail="渠道不存在")
        ch.type = payload.type
        ch.enabled = payload.enabled
        ch.config = payload.config
        ch.min_level = payload.min_level
        # 配置变更后重置失败计数与熔断（用户显然已修正配置）
        ch.fail_count = 0
        ch.breaker_until = None
        if payload.secret:
            ch.secret_enc = cipher.encrypt(
                payload.secret, owner_type="notify_channel", owner_id=payload.name
            )
            ch.name = payload.name
        return _channel_out(ch)


@router.delete("/channels/{channel_id}", summary="删除通知渠道")
async def delete_channel(channel_id: int) -> dict[str, bool]:
    async with session_scope() as session:
        ch = (
            await session.execute(
                select(NotifyChannel).where(NotifyChannel.id == channel_id)
            )
        ).scalar_one_or_none()
        if ch is None:
            raise HTTPException(status_code=404, detail="渠道不存在")
        await session.delete(ch)
        return {"deleted": True}


@router.post("/channels/{channel_id}/test", summary="渠道连通性自检")
async def test_channel(channel_id: int) -> dict[str, Any]:
    """向该渠道发一条测试消息。密钥即时解密，不落日志。"""
    from app.adapters.notify import build_adapter
    from app.services.factory import decrypt_notify_secret

    async with session_scope() as session:
        ch = (
            await session.execute(
                select(NotifyChannel).where(NotifyChannel.id == channel_id)
            )
        ).scalar_one_or_none()
        if ch is None:
            raise HTTPException(status_code=404, detail="渠道不存在")
        secret = decrypt_notify_secret(ch, ch.secret_enc) if ch.secret_enc else None
        adapter = build_adapter(ch.type, dict(ch.config or {}), secret)
        result = await _send_test(adapter)
    return {"ok": result.ok, "error": result.error}


async def _send_test(adapter: object) -> Any:
    """面板通知中心没有 test()，退化为直接记一条。"""
    tester = getattr(adapter, "test", None)
    if tester is None:
        return SendResult(True)
    return await tester()


def _channel_out(ch: NotifyChannel) -> ChannelOut:
    return ChannelOut(
        id=ch.id,
        name=ch.name,
        type=ch.type,
        enabled=ch.enabled,
        min_level=ch.min_level,
        config=dict(ch.config or {}),
        has_secret=bool(ch.secret_enc),
        fail_count=ch.fail_count,
        breaker_until=ch.breaker_until,
        last_ok_at=ch.last_ok_at,
        last_error=ch.last_error,
    )

