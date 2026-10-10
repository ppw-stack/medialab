"""站点管理 API（FR-6.1）。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.adapters.sites import resolve as resolve_site_adapter
from app.adapters.sites.base import SiteProfile
from app.api.auth import require_session
from app.core.config import get_settings
from app.core.crypto import build_cipher, mask_secret
from app.core.timeutils import ClockOffset
from app.db.models import Feed, Site
from app.db.session import session_scope

router = APIRouter(prefix="/sites", dependencies=[Depends(require_session)], tags=["sites"])


class SiteCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="字母/数字/下划线/连字符")
    adapter: Literal["mteam", "hhanclub", "hdsky", "ourbits"] = "mteam"
    domain: str | None = None
    rss_url: str | None = None
    apikey: str | None = Field(default=None, max_length=256)
    cookies: str | None = Field(default=None, max_length=4096)
    min_interval_minutes: int = Field(default=60, ge=15)
    enabled: bool = True


class SiteUpdate(BaseModel):
    id: int
    name: str | None = None
    domain: str | None = None
    rss_url: str | None = None
    apikey: str | None = None
    cookies: str | None = None
    min_interval_minutes: int | None = Field(default=None, ge=15)
    enabled: bool | None = None
    adapter: Literal["mteam", "hhanclub", "hdsky", "ourbits"] | None = None


class SiteOut(BaseModel):
    id: int
    name: str
    domain: str | None
    adapter: str
    enabled: bool
    rss_url_masked: str
    has_apikey: bool
    has_cookies: bool
    min_interval_minutes: int
    created_at: str
    updated_at: str


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
        await session.execute(delete(Feed).where(Feed.site_id == site_id))
        await session.delete(site)
    return {"id": site_id}


@router.post("", status_code=201, summary="新增站点")
async def create_site(payload: SiteCreate) -> SiteOut:
    from app.core.crypto import build_cipher

    cipher = build_cipher(get_settings())

    async with session_scope() as session:
        exists = (
            await session.execute(select(Site.id).where(Site.name == payload.name))
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail=f"站点 {payload.name} 已存在")

        site = Site(
            name=payload.name,
            domain=payload.domain or "",
            adapter=payload.adapter,
            min_interval_minutes=payload.min_interval_minutes,
            enabled=payload.enabled,
        )
        if payload.rss_url:
            site.rss_url_enc = cipher.encrypt(
                payload.rss_url, owner_type="site", owner_id=payload.name
            )
        if payload.apikey:
            site.apikey_enc = cipher.encrypt(
                payload.apikey, owner_type="site", owner_id=payload.name
            )
        if payload.cookies:
            site.cookies_enc = cipher.encrypt(
                payload.cookies, owner_type="site", owner_id=payload.name
            )
        session.add(site)
        await session.flush()

        if payload.rss_url or payload.apikey or payload.cookies:
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
    async with session_scope() as session:
        site = (await session.execute(select(Site).where(Site.id == site_id))).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")

        updates = payload.model_dump(exclude={"id", "rss_url", "apikey", "cookies"}, exclude_unset=True)
        for key, value in updates.items():
            setattr(site, key, value)
        if payload.rss_url:
            site.rss_url_enc = cipher.encrypt(
                payload.rss_url, owner_type="site", owner_id=site.name
            )
        if payload.apikey:
            site.apikey_enc = cipher.encrypt(
                payload.apikey, owner_type="site", owner_id=site.name
            )
        if payload.cookies:
            site.cookies_enc = cipher.encrypt(
                payload.cookies, owner_type="site", owner_id=site.name
            )
        return _to_out(site, payload.rss_url)


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
            from app.core.crypto import build_cipher

            raw = build_cipher(get_settings()).try_decrypt(
                site.rss_url_enc, owner_type="site", owner_id=site.name
            )
        return _to_out(site, raw)


@router.post("/{site_id}/test", summary="连通性与窗口解析自检")
async def test_site(site_id: int) -> dict[str, Any]:
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        profile = SiteProfile(name=site.name, domain=site.domain)

    adapter_cls = resolve_site_adapter(site.adapter)
    from app.core.crypto import build_cipher
    apikey = None
    cookies = None
    cipher = build_cipher(get_settings())
    if site.apikey_enc:
        try:
            apikey = cipher.try_decrypt(
                site.apikey_enc, owner_type="site", owner_id=site.name
            )
        except Exception:
            apikey = None
    if site.cookies_enc:
        try:
            cookies = cipher.try_decrypt(
                site.cookies_enc, owner_type="site", owner_id=site.name
            )
        except Exception:
            cookies = None
    adapter = adapter_cls(profile, cookies=cookies, apikey=apikey, clock_offset=ClockOffset.zero())
    try:
        health = await adapter.ping()
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


def _to_out(site: Site, raw: str | None) -> SiteOut:
    return SiteOut(
        id=site.id,
        name=site.name,
        domain=site.domain or None,
        adapter=site.adapter,
        enabled=site.enabled,
        rss_url_masked=mask_secret(raw or (site.rss_url_enc or ""), keep=8),
        has_apikey=bool(site.apikey_enc),
        has_cookies=bool(site.cookies_enc),
        min_interval_minutes=site.min_interval_minutes,
        created_at=site.created_at.isoformat() if site.created_at else "",
        updated_at=site.updated_at.isoformat() if site.updated_at else "",
    )
