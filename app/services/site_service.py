"""站点管理服务（站点 CRUD + 自检 + 拉取测试）。

此前这些逻辑全在 :mod:`app.api.routes.sites` 路由层：11 处
``session_scope`` 直写 SQL，外加路由层**直接构造适配器**（测连 / 拉取测试
两处）——业务逻辑漏进路由层，路由无法脱离 DB 与网络单测。

本模块把业务逻辑下沉，路由只留「参数校验 + 结果转换」。

分三部分：

1. **CRUD**（:func:`list_sites` / :func:`get_site` / :func:`create_site` /
   :func:`update_site` / :func:`delete_site`）——纯 DB 操作
2. **自检**（:func:`self_test_site`）——连通性 + 窗口解析探针
3. **拉取测试**（:func:`test_site_feed`）——真实拉一次种子列表

错误一律抛 :class:`app.services.errors.ServiceError`（带 HTTP 状态码），
由 API 层统一翻译。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Final, cast

from sqlalchemy import CursorResult, delete, select

from app.adapters.sites import resolve as resolve_site_adapter
from app.adapters.sites.base import FeedItem, SiteHealth, SiteProfile
from app.core.logging import get_logger
from app.db.models import Site, SiteUserData, TaskQueue, TorrentIndex
from app.db.session import session_scope
from app.services.errors import ServiceError
from app.services.factory import AdapterFactory, get_factory, invalidate_site
from app.services.feed_fetcher import FeedFetcher
from app.services.task_queue import TaskKind
from app.services.window_parser import WindowParser

logger = get_logger(__name__)

__all__ = [
    "UNSET",
    "AdapterInfo",
    "SiteFeedTestResult",
    "SiteSelfTestResult",
    "SiteSnapshot",
    "SiteTestResult",
    "SiteUserDataSnapshot",
    "create_site",
    "delete_site",
    "get_site",
    "get_site_user_data",
    "list_adapters",
    "list_sites",
    "refresh_site_user_data",
    "self_test_site",
    "test_all_sites",
    "test_site_feed",
    "update_site",
    "validate_adapter",
]


class _Unset:
    """哨兵类型：区分「未提供该字段」与「显式传 None」。

    三态语义在凭据字段上是必需的：

    - **未提供** → 不动 DB（保留原值）
    - **空串 / None** → 清空该字段
    - **有值** → 覆盖

    只用 ``None`` 表示「未提供」的话，就无法表达「显式清空」——
    前端编辑弹窗保存时就是这么传的（``apikey: ''`` 表示清空）。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: 「未提供该字段」的哨兵值。
UNSET: Final = _Unset()


# ==================================================================== DTO


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """前端 dropdown 用的 adapter 展示信息。"""

    name: str
    label: str
    domain: str


@dataclass(frozen=True, slots=True)
class SiteSnapshot:
    """站点配置快照（对外 DTO，敏感字段已脱敏）。"""

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


@dataclass(frozen=True, slots=True)
class SiteUserDataSnapshot:
    """站点用户数据（4 字段 + 时魔 + 站点名）。"""

    site_id: int
    site_name: str
    has_data: bool
    upload: int = 0
    download: int = 0
    seeding_count: int = 0
    leeching_count: int = 0
    #: 时魔（每小时获得的站点积分·合计口径）
    hourly_bonus: float | None = None
    #: 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分）
    hourly_bonus_unit: str | None = None
    #: 是否已达该站的获取上限（None = 该站无法判断）
    hourly_bonus_capped: bool | None = None
    #: 站点进度百分比
    hourly_bonus_progress: float | None = None
    last_seen_at: str | None = None


@dataclass(frozen=True, slots=True)
class SiteSelfTestResult:
    """站点自检结果（连通性 + 窗口解析探针）。"""

    reachable: bool
    logged_in: bool
    detail: str
    free_type: str
    free_end_at: str | None
    free_source: str
    confidence: float


@dataclass(frozen=True, slots=True)
class SiteFeedTestResult:
    """站点种子列表拉取测试结果。"""

    ok: bool
    status: str
    item_count: int = 0
    duration_ms: int = 0
    sample_titles: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True, slots=True)
class SiteTestResult:
    """单站测连结果（``test_all_sites`` 返回的元素）。

    与 :class:`SiteSelfTestResult` 的区别：这个只含连通性三字段，不含
    窗口解析探针——「全部测连」只关心站点通不通，窗口解析是单站自检的事。
    """

    site_id: int
    name: str
    reachable: bool
    logged_in: bool
    detail: str


# ==================================================================== 工具


def _mask_rss_url(url: str | None) -> str:
    """掩码 RSS URL 中的凭据参数（passkey/apikey/key/auth），防泄漏到前端。

    注意：``key`` 必须带 ``=`` 才匹配，所以 ``keyword=`` / ``keys=`` 等
    含 "key" 的正常参数名不会被误伤（正则要求参数名整体等于 key）。
    """
    if not url:
        return ""
    return re.sub(r"(?i)((?:passkey|apikey|key|auth)=)[^&]+", r"\1***", url)


def _to_snapshot(site: Site) -> SiteSnapshot:
    return SiteSnapshot(
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


def validate_adapter(adapter: str) -> None:
    """校验 adapter 名在注册中心里。

    合法名单的**唯一来源**是 :mod:`app.adapters.sites._registry`
    （``_BUILTIN``）。之前 API 层用 pydantic ``Literal[...]`` 硬编码了
    第 3 份名单，漏改不会报错——加站点要改 4 处。

    改用运行时校验后，名单只在注册中心维护 1 份。

    :raises ServiceError: 400（未注册的 adapter 名）
    """
    from app.adapters.sites._registry import list_available

    if adapter not in list_available():
        raise ServiceError(
            f"未注册的站点 adapter：{adapter!r}，可选：{list_available()}",
            status=400,
        )


def list_adapters() -> list[AdapterInfo]:
    """返回所有内置 adapter 的 ``{name, label, domain}``。

    单一数据源：后端 :mod:`app.adapters.sites._registry`。
    """
    from app.adapters.sites._registry import list_meta

    return [
        AdapterInfo(name=name, label=meta.label, domain=meta.domain)
        for name, meta in list_meta()
    ]


# ==================================================================== CRUD


async def list_sites() -> list[SiteSnapshot]:
    """所有站点（按 id 升序）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(select(Site).order_by(Site.id.asc()))
        ).scalars().all()
    return [_to_snapshot(s) for s in rows]


async def get_site(site_id: int) -> SiteSnapshot:
    """单站点详情。

    :raises ServiceError: 404（站点不存在）
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ServiceError("站点不存在", status=404)
        return _to_snapshot(site)


async def create_site(
    *,
    name: str,
    adapter: str,
    domain: str | None = None,
    rss_url: str | None = None,
    apikey: str | None = None,
    cookies: str | None = None,
    enabled: bool = True,
) -> SiteSnapshot:
    """新增站点。

    :raises ServiceError: 400（未注册的 adapter）/ 409（同名）
    """
    validate_adapter(adapter)
    async with session_scope() as session:
        exists = (
            await session.execute(select(Site.id).where(Site.name == name))
        ).first()
        if exists:
            raise ServiceError(f"站点 {name} 已存在", status=409)

        site = Site(
            name=name,
            domain=domain or "",
            adapter=adapter,
            enabled=enabled,
            rss_url=rss_url,
            apikey=apikey,
            cookies=cookies,
        )
        session.add(site)
        await session.flush()
        # 凭据可后续通过 update_site 补齐
        return _to_snapshot(site)


async def update_site(
    site_id: int,
    *,
    name: str | None = None,
    domain: str | None = None,
    rss_url: str | _Unset | None = UNSET,
    apikey: str | _Unset | None = UNSET,
    cookies: str | _Unset | None = UNSET,
    enabled: bool | None = None,
    adapter: str | None = None,
) -> SiteSnapshot:
    """更新站点配置。

    凭据字段（``rss_url`` / ``apikey`` / ``cookies``）三态：

    - ``UNSET``（默认）→ 不动 DB
    - ``""`` / ``None`` → 清空该字段
    - 有值 → 覆盖

    其余字段 ``None`` = 不修改。

    :raises ServiceError: 400（未注册的 adapter）/ 404（站点不存在）/ 409（同名）
    """
    if adapter is not None:
        validate_adapter(adapter)

    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ServiceError("站点不存在", status=404)

        # 改名撞名预检（避免唯一约束冲突 → 500 + SQL 参数泄漏）
        if name is not None and name != site.name:
            dup = (
                await session.execute(
                    select(Site.id).where(Site.name == name, Site.id != site_id)
                )
            ).first()
            if dup:
                raise ServiceError(f"站点 {name} 已存在", status=409)

        if name is not None:
            site.name = name
        if domain is not None:
            # domain 列是 NOT NULL——前端可能传 null（列表页 domain 可空），
            # 归一为 "" 与 create_site 一致，避免 IntegrityError → 500。
            site.domain = domain or ""
        if enabled is not None:
            site.enabled = enabled
        if adapter is not None:
            site.adapter = adapter

        # 凭据字段：显式给了才处理（空串 / None = 清空）
        for key, value in (
            ("rss_url", rss_url),
            ("apikey", apikey),
            ("cookies", cookies),
        ):
            if not isinstance(value, _Unset):
                setattr(site, key, value or None)

    # 快照在**提交后**取：``updated_at`` 由 ``onupdate`` 在 flush 时写入，
    # 事务内读到的是旧值（提交前）。``expire_on_commit=False`` 保证
    # session 关闭后已加载属性仍可读。
    out = _to_snapshot(site)

    # 配置变了 → 丢弃缓存的适配器实例（否则 cron/推送仍用旧 cookies/apikey）
    invalidate_site(site_id)
    return out


async def delete_site(site_id: int) -> int:
    """删除站点（**级联清理**全部关联数据）。

    关联数据三张表，全在**同一事务**里删（顺序：先子表后父表）：

    - ``torrent_index`` —— 该站的**拉取快照**（种子列表缓存）
    - ``site_user_data`` —— 该站的用户数据快照
    - ``task_queue`` —— 该站遗留的推送队列行（见下）

    **为什么级联而不是 409 拦住**（2026-10-23 改）：

    原实现是「站点下还有 N 条任务，请先清理」→ 409。这是个死胡同：

    1. ``torrent_index`` **不是任务表**，是 ``fetch_all_sites`` 每 10 分钟
       upsert 的**可重建缓存**（"任务"是旧实现遗留的误称——那时拦的是
       已下线的 ``Torrent`` / ``FreeCandidate`` 真任务表）
    2. 全仓**没有**删除 ``torrent_index`` 行的入口（索引页无删除按钮、
       ``index_service`` 零 delete、API 也没有）→ "请先清理"做不到
    3. 就算手动清了，``fetch_all_sites`` 下一轮（10 分钟内）又灌满
       → 该 409 对**任何被拉取过**的站点都是永久性的

    同一个 bug 在下载器那边已修过（``7fff1d0``："删除下载器的引用计数
    检查删除 …… 原检查统计的是全局已推送数，导致任何下载器都删不掉"），
    站点这边是同一类"引用计数拦住了不该拦的删除"。

    **误删风险评估**：``torrent_index`` 100% 可由拉取作业重建。唯一丢失
    的是该站 ``push_status=pushed`` 标记，影响面是优雅降级——
    ``push_free`` 不再给该站产候选；``monitor.py`` 按 hash 匹配不到这些
    种子 → 计入 ``skipped_unmatched``（跳过，不是误暂停）。

    **并发 fetch 竞态**：删站瞬间若有一个 fetch 已读完站点列表、还在网络
    拉取，它回来 upsert 时会撞 FK → ``IntegrityError``。已实测 WAL +
    ``foreign_keys=ON`` 下 FK 会**拒绝**孤儿行写入（不会泄漏脏数据），
    该失败被 ``_upsert_site_items`` 的 savepoint 兜住（只记 debug + 计
    一次写失败），下一轮就干净了。

    **task_queue 为什么一起删**：``idem_key`` 格式是
    ``add_torrent|{site_id}|{item_id}|dl={dl}``。SQLite 删行后**会复用
    id**——新建站点可能拿到旧站的 id，若 item_id 撞上（m-team 是站内
    数字 id），老队列行会让推送被误判成"已推过"而永久跳过。删除时一并
    清掉该站的队列行，消除这个隐患（这些行 7 天后本来也会被
    ``drain_task_queue`` 的 ``purge_finished`` 回收）。

    :raises ServiceError: 404（站点不存在）
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ServiceError("站点不存在", status=404)
        site_name = site.name

        # 1) 拉取快照（torrent_index.site_id → sites.id，无 ON DELETE CASCADE）
        index_count = int(
            cast(
                CursorResult[object],
                await session.execute(
                    delete(TorrentIndex).where(TorrentIndex.site_id == site_id)
                ),
            ).rowcount
            or 0
        )

        # 2) 用户数据快照（site_user_data.site_id → sites.id，UNIQUE）
        ud_count = int(
            cast(
                CursorResult[object],
                await session.execute(
                    delete(SiteUserData).where(SiteUserData.site_id == site_id)
                ),
            ).rowcount
            or 0
        )

        # 3) 该站的推送队列行（idem_key 前缀 = "add_torrent|{site_id}|"）
        #    payload 里的 index_id 不是外键，但 idem_key 里的 site_id 是
        #    ——SQLite 会复用被删行的 id，留着会让新站误命中老幂等键。
        #    前缀用 TaskKind.ADD_TORRENT 常量而非字面量（落库的持久化值）。
        queue_prefix = f"{TaskKind.ADD_TORRENT}|{site_id}|"
        queue_count = int(
            cast(
                CursorResult[object],
                await session.execute(
                    delete(TaskQueue).where(
                        TaskQueue.idem_key.startswith(queue_prefix)
                    )
                ),
            ).rowcount
            or 0
        )

        # 4) 父行（子行已清空 → FK 不会拦）
        await session.delete(site)

    # 审计：删数据不可恢复，记站点名 + 各表删除行数（AGENTS.md 要求）
    logger.warning(
        "删除站点 %s（id=%d）：拉取索引 %d 条、用户数据 %d 条、队列任务 %d 条",
        site_name, site_id, index_count, ud_count, queue_count,
    )
    invalidate_site(site_id)
    return site_id


# ==================================================================== 用户数据


async def get_site_user_data(site_id: int) -> SiteUserDataSnapshot:
    """取一站最近一次 user_data 行（FR-1.6 精简版）。

    :raises ServiceError: 404（站点不存在）
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ServiceError("站点不存在", status=404)
        ud = (
            await session.execute(
                select(SiteUserData).where(SiteUserData.site_id == site_id)
            )
        ).scalar_one_or_none()
        if ud is None:
            return SiteUserDataSnapshot(
                site_id=site_id, site_name=site.name, has_data=False
            )
        return SiteUserDataSnapshot(
            site_id=site_id,
            site_name=site.name,
            has_data=True,
            upload=ud.upload,
            download=ud.download,
            seeding_count=ud.seeding_count,
            leeching_count=ud.leeching_count,
            hourly_bonus=ud.hourly_bonus,
            hourly_bonus_unit=ud.hourly_bonus_unit,
            hourly_bonus_capped=ud.hourly_bonus_capped,
            hourly_bonus_progress=ud.hourly_bonus_progress,
            last_seen_at=ud.last_seen_at,
        )


async def refresh_site_user_data(
    site_id: int, factory: AdapterFactory | None = None
) -> bool:
    """立即调 :meth:`SiteAdapter.get_user_snapshot` → 写 site_user_data。

    **网络 IO 在事务外**：先拉快照，再开短事务 upsert——避免写锁横跨
    整个 HTTP 请求（阻塞 cron 与其他写）。

    :raises ServiceError: 404（站点不存在）
    """
    from app.services.user_data import refresh_site_by_id

    factory = factory or get_factory()
    # 先确认站点存在（404 由这里给出，避免下游抛 KeyError）
    async with session_scope() as session:
        exists = (
            await session.execute(select(Site.id).where(Site.id == site_id))
        ).first()
    if exists is None:
        raise ServiceError("站点不存在", status=404)

    return await refresh_site_by_id(factory, site_id)


# ==================================================================== 自检


async def self_test_site(site_id: int) -> SiteSelfTestResult:
    """连通性与窗口解析自检。

    连通性走适配器的 ``check_reachability``；窗口解析用一段固定文案做
    探针，验证 :class:`app.services.window_parser.WindowParser` 链路正常。

    注：这里**不复用**工厂缓存的适配器——自检要用当前 DB 里的最新
    cookies/apikey，且自检完即关连接，不该污染缓存池。

    :raises ServiceError: 404（站点不存在）/ 400（adapter 未注册）
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ServiceError("站点不存在", status=404)
        profile = SiteProfile(name=site.name, domain=site.domain)
        adapter_name = site.adapter
        cookies = site.cookies
        apikey = site.apikey

    try:
        adapter_cls = resolve_site_adapter(adapter_name)
    except (ValueError, TypeError) as exc:
        raise ServiceError(str(exc), status=400) from exc

    adapter = adapter_cls(profile, cookies=cookies, apikey=apikey)
    try:
        health: SiteHealth = await adapter.check_reachability()
        # 业务层 WindowParser.parse() 自检：用一段"剩余 2 天 6 小时"文案
        probe_item = FeedItem(
            title="[self-test] 剩余 2 天 6 小时",
            description="剩余 2 天 6 小时",
        )
        probe = WindowParser.parse(probe_item)
        return SiteSelfTestResult(
            reachable=health.reachable,
            logged_in=health.logged_in,
            detail=health.detail,
            free_type=probe.free_type,
            free_end_at=probe.free_end_at,
            free_source=probe.free_source,
            confidence=probe.confidence,
        )
    finally:
        await adapter.close()


# ==================================================================== 全部测连


#: 全部测连的并发上限。
#:
#: 站点测连是 GET 首页（轻量），但站点对短时间多次请求敏感（频控 / 封禁），
#: 故仍限并发——与 ``refresh_user_data`` 的上限 5 同量级，取 3 保守。
_TEST_ALL_CONCURRENCY = 3


async def test_all_sites() -> list[SiteTestResult]:
    """对所有**启用**的站点并发测连。

    **只测 enabled=True 的站点**（与 ``fetch_all_sites`` 作业一致）——
    停用的站点不参与拉取，测它没意义。

    **单站失败不中断**：某站抛 ``ServiceError`` / Exception → 折叠成
    ``reachable=False`` 的结果，其余站点继续测。部分站点失败不该让整批失败。

    **并发上限** ``_TEST_ALL_CONCURRENCY``：站点对短时间多次请求敏感，
    限并发避免触发频控。
    """
    # 短事务读启用站点列表（网络 IO 在事务外）
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Site).where(Site.enabled.is_(True)).order_by(Site.id)
            )
        ).scalars().all()
        site_specs = [(row.id, row.name) for row in rows]

    sem = asyncio.Semaphore(_TEST_ALL_CONCURRENCY)

    async def _test_one(site_id: int, name: str) -> SiteTestResult:
        async with sem:
            try:
                r = await self_test_site(site_id)
                return SiteTestResult(
                    site_id=site_id,
                    name=name,
                    reachable=r.reachable,
                    logged_in=r.logged_in,
                    detail=r.detail,
                )
            except ServiceError as exc:
                return SiteTestResult(
                    site_id=site_id,
                    name=name,
                    reachable=False,
                    logged_in=False,
                    detail=str(exc),
                )
            except Exception as exc:
                logger.exception("测连站点 %s 异常", name)
                return SiteTestResult(
                    site_id=site_id,
                    name=name,
                    reachable=False,
                    logged_in=False,
                    detail=f"内部错误：{exc}",
                )

    return await asyncio.gather(
        *[_test_one(sid, name) for sid, name in site_specs]
    )


# ==================================================================== 拉取测试


async def test_site_feed(site_id: int) -> SiteFeedTestResult:
    """测试站点连通性与解析结果（走 adapter.search_torrents）。

    **不落库、不触发 cron**——纯诊断，避免用户"测试"操作意外添加任务。

    :raises ServiceError: 404（站点不存在）
    """
    async with session_scope() as session:
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ServiceError("站点不存在", status=404)
        site_name = site.name
        site_row = site

    factory = get_factory()
    try:
        url = factory.site_rss_url(site_row)
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        logger.warning("站点 %s 凭据解析失败：%s", site_name, exc)
        return SiteFeedTestResult(ok=False, status="credential_error", error=str(exc))
    except Exception:
        logger.exception("站点 %s 站点测试内部异常", site_name)
        return SiteFeedTestResult(
            ok=False, status="internal_error", error="站点 URL 生成失败"
        )

    adapter = factory.site(site_row)
    fetcher = FeedFetcher(site_name)
    result = await fetcher.fetch(adapter, url)
    return SiteFeedTestResult(
        ok=result.ok,
        status=str(result.status),
        item_count=len(result.items),
        duration_ms=result.duration_ms,
        sample_titles=[item.title for item in result.items[:5]],
        error=result.error,
    )
