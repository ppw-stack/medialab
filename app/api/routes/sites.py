"""站点管理 API（FR-6.1）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.adapters.sites import resolve as resolve_site_adapter
from app.adapters.sites.base import SiteProfile
from app.api.auth import require_session
from app.core.config import get_settings
from app.core.crypto import build_cipher, mask_secret
from app.core.timeutils import ClockOffset, parse_duration_seconds
from app.db.models import Feed, Site
from app.db.session import session_scope

router = APIRouter(prefix="/sites", dependencies=[Depends(require_session)], tags=["sites"])


class SiteCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="字母/数字/下划线/连字符")
    domain: str
    rss_url: str | None = None
    adapter: Literal["mteam"] = "mteam"  # FR-1.5 站点 adapter 名（注册中心约束）
    save_path_template: str = "/data/torrents/{site}/{name}"
    max_size: int | None = Field(default=None, description="辅种体积上限（字节）")
    min_interval_minutes: int = Field(default=60, ge=15, description="FR-2.2 下限 15")
    downloader_id: int | None = None
    category: str = "cross-seed"
    default_free_duration: str = "72h"
    min_buffer: str = "30m"
    buffer_ratio: float = 0.10
    detail_path_template: str | None = None
    rss_time_pattern: str | None = None
    grab_short_window: bool = False
    enabled: bool = True


class SiteUpdate(BaseModel):
    id: int  # 必填：标识要更新的站点
    name: str | None = None
    domain: str | None = None
    rss_url: str | None = None  # 留空字符串表示不修改
    adapter: str | None = None
    save_path_template: str | None = None
    max_size: int | None = None
    min_interval_minutes: int | None = Field(default=None, ge=15)
    downloader_id: int | None = None
    category: str | None = None
    default_free_duration: str | None = None
    min_buffer: str | None = None
    buffer_ratio: float | None = None
    detail_path_template: str | None = None
    rss_time_pattern: str | None = None
    grab_short_window: bool | None = None
    enabled: bool | None = None
    adapter: Literal["mteam"] | None = None  # 改名时也允许提供


class SiteOut(BaseModel):
    id: int
    name: str
    domain: str
    adapter: str
    enabled: bool
    rss_url_masked: str
    default_free_duration: int
    min_buffer: int
    buffer_ratio: float
    clock_offset: int
    clock_offset_probed_at: datetime | None
    rescue_strategy: str
    grab_short_window: bool


@router.get("", summary="站点列表（凭据脱敏）")
async def list_sites() -> list[SiteOut]:
    async with session_scope() as session:
        rows = (await session.execute(select(Site))).scalars().all()
    return [_to_out(s, raw=None) for s in rows]


@router.post("/delete", summary="删除站点")
async def delete_site(payload: dict) -> dict[str, int]:
    site_id = payload.get("id") if isinstance(payload, dict) else None
    if site_id is None:
        raise HTTPException(status_code=400, detail="id 必填")
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        # 级联删除关联的 feeds
        await session.execute(delete(Feed).where(Feed.site_id == site_id))
        await session.delete(site)
    return {"id": site_id}


@router.post("", status_code=201, summary="新增站点")
async def create_site(payload: SiteCreate) -> SiteOut:
    from app.core.config import get_settings
    from app.core.crypto import build_cipher

    settings = get_settings()
    cipher = build_cipher(settings)

    async with session_scope() as session:
        exists = (
            await session.execute(select(Site.id).where(Site.name == payload.name))
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail=f"站点 {payload.name} 已存在")

        duration = parse_duration_seconds(payload.default_free_duration)
        buffer = parse_duration_seconds(payload.min_buffer)
        site = Site(
            name=payload.name,
            domain=payload.domain,
            adapter=payload.adapter,
            save_path_template=payload.save_path_template,
            max_size=payload.max_size,
            min_interval_minutes=payload.min_interval_minutes,
            downloader_id=payload.downloader_id,
            category=payload.category,
            default_free_duration=int(duration.total_seconds()) if duration else 72 * 3600,
            min_buffer=int(buffer.total_seconds()) if buffer else 1800,
            buffer_ratio=payload.buffer_ratio,
            detail_path_template=payload.detail_path_template,
            rss_time_pattern=payload.rss_time_pattern,
            grab_short_window=payload.grab_short_window,
            enabled=payload.enabled,
            key_version=settings.key_version,
        )
        if payload.rss_url:
            site.rss_url_enc = cipher.encrypt(
                payload.rss_url, owner_type="site", owner_id=payload.name
            )
        session.add(site)
        await session.flush()

        # RSS 源随站点一同创建：引擎只按 feeds 表调度，没有这条记录
        # 站点就永远不会被拉取（FR-2.2）
        if payload.rss_url:
            session.add(
                Feed(
                    site_id=site.id,
                    kind="site_rss",
                    interval_minutes=payload.min_interval_minutes,
                    enabled=payload.enabled,
                )
            )
        return _to_out(site, payload.rss_url)


@router.post("/update", summary="更新站点")
async def update_site(payload: SiteUpdate) -> SiteOut:
    """更新站点配置（id 在 body）。rss_url 留空字符串 = 不修改。"""
    site_id = payload.id
    cipher = build_cipher(get_settings())
    settings = get_settings()
    async with session_scope() as session:
        site = (await session.execute(select(Site).where(Site.id == site_id))).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")

        updates = payload.model_dump(exclude={"id", "rss_url"}, exclude_unset=True)
        for key, value in updates.items():
            setattr(site, key, value)
        if payload.default_free_duration is not None:
            duration = parse_duration_seconds(payload.default_free_duration)
            if duration is not None:
                site.default_free_duration = int(duration.total_seconds())
        if payload.min_buffer is not None:
            buffer = parse_duration_seconds(payload.min_buffer)
            if buffer is not None:
                site.min_buffer = int(buffer.total_seconds())
        # rss_url：空字符串视为"不修改"，非空才覆盖加密
        if payload.rss_url:
            site.rss_url_enc = cipher.encrypt(
                payload.rss_url, owner_type="site", owner_id=site.name
            )
        await session.flush()
        return _to_out(site, None)


@router.get("/{site_id}", summary="站点详情")
async def get_site(site_id: int) -> SiteOut:
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        raw = None
        if site.rss_url_enc:
            from app.core.config import get_settings
            from app.core.crypto import build_cipher

            raw = build_cipher(get_settings()).try_decrypt(
                site.rss_url_enc, owner_type="site", owner_id=site.name
            )
        return _to_out(site, raw)


@router.post("/{site_id}/test", summary="连通性与窗口解析自检")
async def test_site(site_id: int) -> dict[str, Any]:
    """站点适配层自检（FR-1.5.4）。验证配置可用且窗口可解析。"""
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        profile = _profile_from_site(site)
        offset = ClockOffset(offset=timedelta(seconds=site.clock_offset))

    adapter_cls = resolve_site_adapter(site.adapter)
    adapter = adapter_cls(profile, clock_offset=offset)
    try:
        health = await adapter.ping()
        # 字符串路径：mteam 也保留"剩余 X" 文案兜底，便于搜索结果页场景
        probe = await adapter.parse_window("剩余 2 天 6 小时")
        return {
            "reachable": health.reachable,
            "logged_in": health.logged_in,
            "detail": health.detail,
            "capabilities": sorted(str(f) for f in adapter.capabilities()),
            "self_test": {
                "free_type": probe.free_type,
                "free_end_at": probe.free_end_at,
                "free_source": probe.free_source,
                "confidence": probe.confidence,
            },
        }
    finally:
        await adapter.close()


class ClockAdjust(BaseModel):
    """站点时钟手动校正（FR-3.0.2 第 2 条）。"""

    delta_seconds: int = Field(..., description="站点时间 - 本地时间，正值表示站点偏快")


@router.post("/{site_id}/clock-offset", summary="手动校正站点时钟偏差（FR-3.0.2）")
async def adjust_clock_offset(site_id: int, payload: ClockAdjust) -> dict[str, Any]:
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        site.clock_offset += payload.delta_seconds
        site.clock_offset_probed_at = None
        return {"site_id": site_id, "clock_offset": site.clock_offset}


def _profile_from_site(site: Site) -> SiteProfile:
    return SiteProfile(
        name=site.name,
        domain=site.domain,
        detail_path_template=site.detail_path_template,
        rss_time_pattern=site.rss_time_pattern,
        window_field_map=site.window_field_map or {},
        free_type_map={str(k): str(v) for k, v in (site.free_type_map or {}).items()},
        download_factor_map={str(k): str(v) for k, v in (site.download_factor_map or {}).items()},
        time_unit_multiplier=site.time_unit_multiplier,
    )


def _to_out(site: Site, raw: str | None) -> SiteOut:
    return SiteOut(
        id=site.id,
        name=site.name,
        domain=site.domain,
        adapter=site.adapter,
        enabled=site.enabled,
        rss_url_masked=mask_secret(raw or (site.rss_url_enc or ""), keep=8),
        default_free_duration=site.default_free_duration,
        min_buffer=site.min_buffer,
        buffer_ratio=site.buffer_ratio,
        clock_offset=site.clock_offset,
        clock_offset_probed_at=site.clock_offset_probed_at,
        rescue_strategy=site.rescue_strategy,
        grab_short_window=site.grab_short_window,
    )