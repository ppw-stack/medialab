"""站点管理 API（FR-6.1）。

路由层只做三件事：**参数校验 → 调 service → 结果转换**。

业务逻辑（DB 查询、适配器构造、网络调用）全在
:mod:`app.services.site_service`。服务层抛
:class:`app.services.errors.ServiceError`，由全局异常处理器（见
:mod:`app.api.errors`）转成对应的 HTTP 状态码。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.auth import require_session
from app.services import site_service
from app.services.site_service import UNSET

router = APIRouter(prefix="/sites", dependencies=[Depends(require_session)], tags=["sites"])


class SiteCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="字母/数字/下划线/连字符")
    #: 站点适配器名。合法值由 :mod:`app.adapters.sites._registry` 决定，
    #: 在 service 层做运行时校验（**不**用 pydantic Literal——
    #: 那会让 9 站名单在这里维护第 3 份，漏改不会报错）。
    adapter: str = Field(default="generic", max_length=64)
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
    #: 同 :class:`SiteCreate.adapter`——运行时校验
    adapter: str | None = Field(default=None, max_length=64)


class SiteDeleteIn(BaseModel):
    id: int = Field(..., ge=1)


class AdapterInfoOut(BaseModel):
    """前端 dropdown 用的 adapter 展示信息。"""

    name: str
    label: str
    domain: str


class SiteRuleOut(BaseModel):
    """规则包里的站点（前端「通用适配器」加站时选择）。"""

    id: str
    name: str
    domain: str
    #: 用户数据解析框架；None = 该站只做搜索
    userdata_schema: str | None = None
    #: movie / tv / music；None = 综合站
    media_type: str | None = None
    has_subtitles: bool = False


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
    # -- 用户数据（FR-1.6 快照；has_data=false 时下面各值为默认）--
    has_data: bool = False
    upload: int = 0
    download: int = 0
    seeding_count: int = 0
    user_data_seen_at: str | None = None


class SiteUserDataOut(BaseModel):
    """站点用户数据（FR-1.6 精简版，3 字段）。"""

    site_id: int
    site_name: str
    has_data: bool
    upload: int = 0
    download: int = 0
    seeding_count: int = 0
    #: 是否已达该站的获取上限（None = 该站无法判断）
    #: 站点进度百分比
    last_seen_at: str | None = None


class SiteSelfTestOut(BaseModel):
    """连通性与窗口解析自检结果。"""

    reachable: bool
    logged_in: bool
    detail: str
    self_test: dict[str, Any]


class FeedTestItemOut(BaseModel):
    """拉取测试里的一条解析结果。"""

    title: str
    item_id: str | None = None
    size: int = 0
    seeders: int = 0
    peers: int = 0
    category: str | None = None
    published_at: str | None = None
    download_factor: float | None = None


class FeedTestResult(BaseModel):
    """站点连通性测试结果。"""

    ok: bool
    status: str
    item_count: int = 0
    duration_ms: int = 0
    sample_titles: list[str] = []
    items: list[FeedTestItemOut] = []
    error: str = ""


class SiteTestItemOut(BaseModel):
    """单站测连结果（``test-all`` 返回的元素）。"""

    site_id: int
    name: str
    reachable: bool
    logged_in: bool
    detail: str


class SiteTestAllOut(BaseModel):
    """全部测连结果。"""

    results: list[SiteTestItemOut]
    summary: dict[str, int]


@router.get("/adapters", summary="列出已注册站点 adapter（前端下拉用）")
async def list_adapters() -> list[AdapterInfoOut]:
    """返回所有内置 adapter 的 ``{name, label, domain}``。

    前端 ``/sites`` 页面在 mount 时拉这个端点，构造「新增站点」modal 里
    的适配器下拉。单一数据源：后端 :mod:`app.adapters.sites._registry`。
    """
    return [
        AdapterInfoOut(name=a.name, label=a.label, domain=a.domain)
        for a in site_service.list_adapters()
    ]


@router.get("/rules", summary="列出规则包里的站点（通用适配器加站用）")
async def list_site_rules() -> list[SiteRuleOut]:
    """返回随包分发的站点规则目录（只读，不含选择器配置）。"""
    return [SiteRuleOut(**asdict(rule)) for rule in site_service.list_site_rules()]


@router.post("/test-all", summary="全部站点测连")
async def test_all_sites() -> SiteTestAllOut:
    """对所有**启用**的站点并发测连（连通性 + 登录态）。

    **只测 enabled=True 的站点**——停用的不参与拉取，测它没意义。
    单站失败不中断（折叠成 ``reachable=False``）。
    并发上限 3（站点对短时间多次请求敏感）。
    """
    results = await site_service.test_all_sites()
    reachable = sum(1 for r in results if r.reachable)
    # 可达但**未登录**（凭据过期 / 没配 cookie）——与「不可达」是两种故障：
    # 前者站点活着但拿不到数据（ourbits 2026-11-08：凭据过期 10 天，
    # 面板测连一直显示绿点）。单独计数，别混进「可达」里。
    unauthorized = sum(1 for r in results if r.reachable and not r.logged_in)
    return SiteTestAllOut(
        results=[
            SiteTestItemOut(
                site_id=r.site_id,
                name=r.name,
                reachable=r.reachable,
                logged_in=r.logged_in,
                detail=r.detail,
            )
            for r in results
        ],
        summary={
            "total": len(results),
            "reachable": reachable,
            "failed": len(results) - reachable,
            "unauthorized": unauthorized,
        },
    )


@router.get("", summary="站点列表")
async def list_sites() -> list[SiteOut]:
    return [SiteOut(**asdict(s)) for s in await site_service.list_sites()]


@router.post("/delete", summary="删除站点")
async def delete_site(payload: SiteDeleteIn) -> dict[str, int]:
    return {"id": await site_service.delete_site(payload.id)}


@router.post("", status_code=201, summary="新增站点")
async def create_site(payload: SiteCreate) -> SiteOut:
    snap = await site_service.create_site(
        name=payload.name,
        adapter=payload.adapter,
        domain=payload.domain,
        rss_url=payload.rss_url,
        apikey=payload.apikey,
        cookies=payload.cookies,
        enabled=payload.enabled,
    )
    return SiteOut(**asdict(snap))


@router.post("/update", summary="更新站点")
async def update_site(payload: SiteUpdate) -> SiteOut:
    """更新站点配置（id 在 body）。

    ``rss_url`` / ``apikey`` / ``cookies`` 留空字符串 = 清空该项；
    未提供（None） = 不修改。
    """
    fields = payload.model_fields_set
    snap = await site_service.update_site(
        payload.id,
        name=payload.name,
        domain=payload.domain,
        rss_url=payload.rss_url if "rss_url" in fields else UNSET,
        apikey=payload.apikey if "apikey" in fields else UNSET,
        cookies=payload.cookies if "cookies" in fields else UNSET,
        enabled=payload.enabled,
        adapter=payload.adapter,
    )
    return SiteOut(**asdict(snap))


@router.get("/{site_id}", summary="站点详情")
async def get_site(site_id: int) -> SiteOut:
    return SiteOut(**asdict(await site_service.get_site(site_id)))


# ----------------------- FR-1.6 站点用户数据 API（精简版） -----------------------


@router.get("/{site_id}/user-data", summary="站点最近用户数据（3 字段）")
async def get_site_user_data(site_id: int) -> SiteUserDataOut:
    """取一站最近一次 :class:`SiteUserData` 行（FR-1.6 精简版）。

    字段：upload / download / seeding_count / last_seen_at。
    """
    return SiteUserDataOut(**asdict(await site_service.get_site_user_data(site_id)))


@router.post("/{site_id}/user-data/refresh", summary="手动刷新站点用户数据")
async def refresh_site_user_data(site_id: int) -> dict[str, Any]:
    """立即调 :meth:`SiteAdapter.get_user_snapshot` → 写 site_user_data。"""
    ok = await site_service.refresh_site_user_data(site_id)
    return {"ok": ok, "site_id": site_id}


@router.post("/{site_id}/test", summary="连通性与窗口解析自检")
async def test_site(site_id: int) -> SiteSelfTestOut:
    result = await site_service.self_test_site(site_id)
    return SiteSelfTestOut(
        reachable=result.reachable,
        logged_in=result.logged_in,
        detail=result.detail,
        self_test={
            "free_type": result.free_type,
            "free_end_at": result.free_end_at,
            "free_source": result.free_source,
            "confidence": result.confidence,
        },
    )


@router.get("/{site_id}/feed-test", summary="站点种子列表拉取测试")
async def test_feed(site_id: int) -> FeedTestResult:
    """测试站点连通性与解析结果（走 adapter.search_torrents）。

    **不落库、不触发 cron**——纯诊断，避免用户"测试"操作意外添加任务。
    """
    result = await site_service.test_site_feed(site_id)
    return FeedTestResult(
        ok=result.ok,
        status=result.status,
        item_count=result.item_count,
        duration_ms=result.duration_ms,
        sample_titles=result.sample_titles,
        items=[FeedTestItemOut(**asdict(item)) for item in result.items],
        error=result.error,
    )
