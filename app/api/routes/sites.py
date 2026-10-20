"""站点管理 API（FR-6.1）。"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.adapters.sites import resolve as resolve_site_adapter
from app.adapters.sites.base import SiteProfile
from app.api.auth import require_session
from app.core.logging import get_logger
from app.db.models import Site, SiteUserData, TorrentIndex
from app.db.session import session_scope
from app.services.factory import get_factory

logger = get_logger(__name__)

router = APIRouter(prefix="/sites", dependencies=[Depends(require_session)], tags=["sites"])


class SiteCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="字母/数字/下划线/连字符")
    adapter: Literal[
        "mteam", "hhanclub", "hdsky", "ourbits", "audiences",
        "springsunday", "hdhome", "hddolby", "soulvoice",
    ] = "mteam"
    domain: str | None = None
    rss_url: str | None = None
    apikey: str | None = Field(default=None, max_length=256)
    cookies: str | None = Field(default=None, max_length=4096)
    enabled: bool = True


class SiteUpdate(BaseModel):
    id: int
    name: str | None = Field(
        default=None, min_length=1, max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="字母/数字/下划线/连字符",
    )
    domain: str | None = None
    rss_url: str | None = None
    apikey: str | None = None
    cookies: str | None = None
    enabled: bool | None = None
    adapter: (
        Literal[
            "mteam", "hhanclub", "hdsky", "ourbits", "audiences",
            "springsunday", "hdhome", "hddolby", "soulvoice",
        ]
        | None
    ) = None


class SiteDeleteIn(BaseModel):
    id: int = Field(..., ge=1)


class AdapterInfo(BaseModel):
    """前端 dropdown 用的 adapter 展示信息。"""

    name: str
    label: str
    domain: str


@router.get("/adapters", summary="列出已注册站点 adapter（前端下拉用）")
async def list_adapters() -> list[AdapterInfo]:
    """返回所有内置 adapter 的 ``{name, label, domain}``。

    前端 ``/sites`` 页面在 mount 时拉这个端点，构造「新增站点」modal 里
    的适配器下拉。单一数据源：后端 :mod:`app.adapters.sites._registry`。
    """
    from app.adapters.sites._registry import list_meta
    return [
        AdapterInfo(name=name, label=meta.label, domain=meta.domain)
        for name, meta in list_meta()
    ]


class SiteOut(BaseModel):
    id: int
    name: str
    domain: str | None
    adapter: str
    enabled: bool
    rss_url_masked: str
    has_apikey: bool
    has_cookies: bool
    created_at: str
    updated_at: str


@router.get("", summary="站点列表")
async def list_sites() -> list[SiteOut]:
    async with session_scope() as session:
        rows = (await session.execute(select(Site))).scalars().all()
    return [_to_out(s) for s in rows]


@router.post("/delete", summary="删除站点")
async def delete_site(payload: SiteDeleteIn) -> dict[str, int]:
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == payload.id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        # 检查引用：torrent_index 仍依赖 site_id
        torrent_count = (
            await session.execute(
                select(func.count())
                .select_from(TorrentIndex)
                .where(TorrentIndex.site_id == payload.id)
            )
        ).scalar_one()
        if torrent_count:
            raise HTTPException(
                status_code=409,
                detail=f"站点下还有 {torrent_count} 条任务，请先清理",
            )
        # 级联清理站点用户数据（site_user_data 引用 site_id，
        # 不删会因 FK 约束抛 IntegrityError → 500）
        await session.execute(
            delete(SiteUserData).where(SiteUserData.site_id == payload.id)
        )
        await session.delete(site)
    get_factory().invalidate_site(payload.id)
    return {"id": payload.id}


@router.post("", status_code=201, summary="新增站点")
async def create_site(payload: SiteCreate) -> SiteOut:
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
            enabled=payload.enabled,
            rss_url=payload.rss_url,
            apikey=payload.apikey,
            cookies=payload.cookies,
        )
        session.add(site)
        await session.flush()
        # 凭据可后续通过 update_site 补齐
        return _to_out(site)


@router.post("/update", summary="更新站点")
async def update_site(payload: SiteUpdate) -> SiteOut:
    """更新站点配置（id 在 body）。

    ``rss_url`` / ``apikey`` / ``cookies`` 留空字符串 = 清空该项；
    未提供（None） = 不修改。
    """
    site_id = payload.id
    async with session_scope() as session:
        site = (await session.execute(select(Site).where(Site.id == site_id))).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")

        # 改名撞名预检（避免唯一约束冲突 → 500 + SQL 参数泄漏）
        if payload.name is not None and payload.name != site.name:
            dup = (
                await session.execute(
                    select(Site.id).where(
                        Site.name == payload.name, Site.id != site_id
                    )
                )
            ).first()
            if dup:
                raise HTTPException(status_code=409, detail=f"站点 {payload.name} 已存在")

        updates = payload.model_dump(
            exclude={"id", "rss_url", "apikey", "cookies"}, exclude_unset=True
        )
        for key, value in updates.items():
            setattr(site, key, value)

        # 凭据字段：model_fields_set 中显式传了才处理（空串 = 清空）
        for key in ("rss_url", "apikey", "cookies"):
            if key in payload.model_fields_set:
                setattr(site, key, getattr(payload, key) or None)
        out = _to_out(site)
    get_factory().invalidate_site(site_id)
    return out


@router.get("/{site_id}", summary="站点详情")
async def get_site(site_id: int) -> SiteOut:
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        return _to_out(site)


# ----------------------- FR-1.6 站点用户数据 API（精简版） -----------------------


@router.get("/{site_id}/user-data", summary="站点最近用户数据（4 字段）")
async def get_site_user_data(site_id: int) -> dict[str, Any]:
    """取一站最近一次 :class:`SiteUserData` 行（FR-1.6 精简版）。

    字段：upload / download / seeding_count / leeching_count / last_seen_at。
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        ud = (
            await session.execute(
                select(SiteUserData).where(SiteUserData.site_id == site_id)
            )
        ).scalar_one_or_none()
        if ud is None:
            return {
                "site_id": site_id,
                "site_name": site.name,
                "has_data": False,
            }
        return {
            "site_id": site_id,
            "site_name": site.name,
            "has_data": True,
            "upload": ud.upload,
            "download": ud.download,
            "seeding_count": ud.seeding_count,
            "leeching_count": ud.leeching_count,
            "last_seen_at": ud.last_seen_at,
        }


@router.post("/{site_id}/user-data/refresh", summary="手动刷新站点用户数据")
async def refresh_site_user_data(site_id: int) -> dict[str, Any]:
    """立即调 :meth:`SiteAdapter.get_user_snapshot` → 写 site_user_data。"""
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")
        from app.services.factory import get_factory
        from app.services.user_data import refresh_one_site

        factory = get_factory()
        ok = await refresh_one_site(session, factory, site)
    return {"ok": ok, "site_id": site_id}


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
    adapter = adapter_cls(profile, cookies=site.cookies, apikey=site.apikey)
    try:
        health = await adapter.check_reachability()
        # 业务层 WindowParser.parse() 自检：用一段"剩余 2 天 6 小时"文案
        from app.adapters.sites.base import FeedItem
        from app.services.window_parser import WindowParser

        probe_item = FeedItem(
            title="[self-test] 剩余 2 天 6 小时",
            link="",
            description="剩余 2 天 6 小时",
        )
        probe = WindowParser.parse(probe_item)
        return {
            "reachable": health.reachable,
            "logged_in": health.logged_in,
            "detail": health.detail,
            "self_test": {
                "free_type": probe.free_type,
                "free_end_at": probe.free_end_at,
                "free_source": probe.free_source,
                "confidence": probe.confidence,
            },
        }
    finally:
        await adapter.close()


class FeedTestResult(BaseModel):
    """站点连通性测试结果。"""

    ok: bool
    status: str
    item_count: int = 0
    duration_ms: int = 0
    sample_titles: list[str] = []
    error: str = ""


@router.get("/{site_id}/feed-test", summary="站点种子列表拉取测试")
async def test_feed(site_id: int) -> FeedTestResult:
    """测试站点连通性与解析结果（走 adapter.search_torrents）。

    **不落库、不触发 cron**——纯诊断，避免用户"测试"操作意外添加任务。
    """
    from app.services.feed_fetcher import FeedFetcher

    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise HTTPException(status_code=404, detail="站点不存在")

    factory = get_factory()
    try:
        url = factory.site_rss_url(site)
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        logger.warning("站点 %s 凭据解析失败：%s", site.name, exc)
        return FeedTestResult(ok=False, status="credential_error", error=str(exc))
    except Exception:
        logger.exception("站点 %s 站点测试内部异常", site.name)
        return FeedTestResult(ok=False, status="internal_error", error="站点 URL 生成失败")

    adapter = factory.site(site)
    fetcher = FeedFetcher(site.name)
    result = await fetcher.fetch(adapter, url)
    return FeedTestResult(
        ok=result.ok,
        status=str(result.status),
        item_count=len(result.items),
        duration_ms=result.duration_ms,
        sample_titles=[item.title for item in result.items[:5]],
        error=result.error,
    )


def _mask_rss_url(url: str | None) -> str:
    """掩码 RSS URL 中的凭据参数（passkey/apikey/key），防泄漏到前端。"""
    if not url:
        return ""
    return re.sub(r"(?i)((?:passkey|apikey|key|auth)=)[^&]+", r"\1***", url)


def _to_out(site: Site) -> SiteOut:
    return SiteOut(
        id=site.id,
        name=site.name,
        domain=site.domain or None,
        adapter=site.adapter,
        enabled=site.enabled,
        rss_url_masked=_mask_rss_url(site.rss_url),
        has_apikey=bool(site.apikey),
        has_cookies=bool(site.cookies),
        created_at=site.created_at or "",
        updated_at=site.updated_at or "",
    )
