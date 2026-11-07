"""拉取索引查询服务（FR-6.3）。

此前 :mod:`app.api.routes.torrent_index` 的路由层直接写 SQL（6 处
``session_scope``）+ 拼装 DTO。路由既碰 DB 又做结果映射，无法脱离 DB
单测。

本模块下沉两件事：

1. **查询**（:func:`list_index` / :func:`list_index_sites` /
   :func:`list_free_types` / :func:`get_index_entry` /
   :func:`index_stats`）——分页、过滤、排序、统计
2. **推送**（:func:`push_index_entry`）——手动推送到下载器

推送部分复用 :mod:`app.services.push_service`（与自动推送**同一条队列 +
同一个执行器**），本模块只做「解析排序参数 + 调服务 + 转 DTO」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select

from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.services.errors import ServiceError
from app.services.factory import get_factory
from app.services.push_service import (
    PushError,
    PushOutcome,
    enqueue_index_entry,
    push_index_entry,
    resolve_downloader,
)

__all__ = [
    "SORTABLE_COLUMNS",
    "BatchPushItem",
    "BatchPushResult",
    "IndexEntry",
    "IndexPage",
    "IndexStats",
    "get_index_entry",
    "index_stats",
    "list_free_types",
    "list_index",
    "list_index_sites",
    "push_batch",
    "push_entry",
]


#: 允许排序的列白名单（防止任意属性访问）
SORTABLE_COLUMNS: Final[dict[str, Any]] = {
    "name": TorrentIndex.name,
    "size": TorrentIndex.size,
    "category": TorrentIndex.category,
    "free_type": TorrentIndex.free_type,
    "free_end_at": TorrentIndex.free_end_at,
    "push_status": TorrentIndex.push_status,
    "pushed_at": TorrentIndex.pushed_at,
    "published_at": TorrentIndex.published_at,
    "first_seen_at": TorrentIndex.first_seen_at,
    "last_seen_at": TorrentIndex.last_seen_at,
}


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """单条拉取索引条目。"""

    id: int
    site_id: int
    site_name: str
    item_id: str | None
    name: str
    size: int
    category: str | None
    free_type: str
    free_end_at: str | None
    free_source: str | None
    free_confidence: float | None
    discount_pct: int | None
    #: 是否可推送（item_id 非空 → 可调 SiteAdapter.download_torrent 实时拿 bytes）
    pushable: bool
    push_status: str | None
    pushed_at: str | None
    info_hash: str | None
    published_at: str | None
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True)
class IndexPage:
    """分页结果。"""

    total: int
    items: list[IndexEntry]


@dataclass(frozen=True, slots=True)
class IndexStats:
    """拉取索引统计：总条数 + 按 free_type 分布。"""

    total: int
    by_type: dict[str, int]


@dataclass(frozen=True, slots=True)
class SiteOption:
    """站点下拉项。"""

    id: int
    name: str


def _to_entry(row: Any) -> IndexEntry:
    """``(TorrentIndex, site_name)`` 行 → DTO。"""
    ti = row.TorrentIndex
    return IndexEntry(
        id=ti.id,
        site_id=ti.site_id,
        site_name=row.name,
        item_id=ti.item_id,
        name=ti.name,
        size=ti.size,
        category=ti.category,
        free_type=ti.free_type,
        free_end_at=ti.free_end_at,
        free_source=ti.free_source,
        free_confidence=ti.free_confidence,
        discount_pct=ti.discount_pct,
        pushable=ti.item_id is not None,
        push_status=ti.push_status,
        pushed_at=ti.pushed_at,
        info_hash=ti.info_hash,
        published_at=ti.published_at,
        first_seen_at=ti.first_seen_at,
        last_seen_at=ti.last_seen_at,
    )


async def list_index(
    *,
    site_id: int | None = None,
    free_type: str | None = None,
    q: str | None = None,
    sort_by: str = "last_seen_at",
    order: str = "desc",
    offset: int = 0,
    limit: int = 50,
) -> IndexPage:
    """拉取索引分页列表。默认按 ``last_seen_at DESC`` 排序。"""
    stmt = select(TorrentIndex, Site.name).join(Site, Site.id == TorrentIndex.site_id)

    if site_id is not None:
        stmt = stmt.where(TorrentIndex.site_id == site_id)
    if free_type:
        stmt = stmt.where(TorrentIndex.free_type == free_type)
    if q:
        stmt = stmt.where(TorrentIndex.name.contains(q))

    # 排序：sort_by 必须在白名单内；order 只能是 asc/desc
    sort_col = SORTABLE_COLUMNS.get(sort_by, TorrentIndex.last_seen_at)
    sort_dir = order.lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    sort_fn = sort_col.asc() if sort_dir == "asc" else sort_col.desc()
    # 二级排序：id 保证稳定
    stmt = stmt.order_by(sort_fn, TorrentIndex.id.desc())

    count_stmt = select(func.count()).select_from(stmt.subquery())
    async with session_scope() as session:
        total = (await session.execute(count_stmt)).scalar_one() or 0
        rows = (
            await session.execute(stmt.offset(offset).limit(limit))
        ).all()

    return IndexPage(total=int(total), items=[_to_entry(r) for r in rows])


async def list_index_sites() -> list[SiteOption]:
    """站点列表（供前端过滤下拉）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Site.id, Site.name).order_by(Site.id.asc())
            )
        ).all()
    return [SiteOption(id=r[0], name=r[1]) for r in rows]


async def list_free_types() -> list[str]:
    """当前存在的 free_type 取值列表（供前端过滤下拉）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TorrentIndex.free_type)
                .distinct()
                .order_by(TorrentIndex.free_type.asc())
            )
        ).all()
    return [r[0] for r in rows if r[0]]


async def index_stats(*, site_id: int | None = None) -> IndexStats:
    """总条数 + 按 free_type 分布，供前端 stat 卡片使用。

    注意：与列表页 query（关键字 `q`）解耦——统计反映库内全量，避免
    卡片数字与列表筛选相互干扰。
    """
    base = select(TorrentIndex.free_type).where(TorrentIndex.free_type.is_not(None))
    if site_id is not None:
        base = base.where(TorrentIndex.site_id == site_id)

    async with session_scope() as session:
        total_stmt = select(func.count()).select_from(TorrentIndex)
        if site_id is not None:
            total_stmt = total_stmt.where(TorrentIndex.site_id == site_id)
        total = (await session.execute(total_stmt)).scalar_one() or 0
        rows = (await session.execute(base)).all()
    by_type: dict[str, int] = {}
    for (ft,) in rows:
        if not ft:
            continue
        by_type[ft] = by_type.get(ft, 0) + 1
    return IndexStats(total=int(total), by_type=by_type)


async def get_index_entry(index_id: int) -> IndexEntry:
    """单条拉取索引详情。

    :raises ServiceError: 404（条目不存在）
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(TorrentIndex, Site.name)
                .join(Site, Site.id == TorrentIndex.site_id)
                .where(TorrentIndex.id == index_id)
            )
        ).first()
    if row is None:
        raise ServiceError("torrent_index entry not found", status=404)
    return _to_entry(row)


@dataclass(frozen=True, slots=True)
class BatchPushItem:
    """批量推送的单条结果。

    :param state: ``enqueued``（已入队）/ ``skipped_pushed``（已推过）/
        ``skipped_queued``（队列里已有）/ ``skipped_no_link``（无可用链接）/
        ``skipped_missing``（索引不存在）/ ``failed``
    """

    id: int
    state: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BatchPushResult:
    """批量推送汇总。"""

    total: int
    enqueued: int
    skipped: int
    failed: int
    items: list[BatchPushItem]


async def push_batch(
    ids: list[int], downloader_id: int | None = None
) -> BatchPushResult:
    """批量推送：把选中的拉取索引**入队**（**不限制数量**）。

    每条走 :func:`push_service.enqueue_index_entry`——与单条手动推送
    **同一份**校验 / 幂等 / 入队逻辑（不会漂移），但**只入队不执行**。

    **为什么不同步执行**：一次几百条同步执行 = 几百次「下载 .torrent +
    推给下载器」的网络往返，HTTP 请求会挂十几分钟；入队则立即返回，
    且天然享受队列的幂等 / 退避 / 崩溃恢复 / 下载器友好节流
    （``drain_task_queue`` 每轮 50 条、条间 2s）。执行时由
    :func:`app.services.task_executor.execute_add_torrent` 更新
    ``push_status``（TOCTOU 防御在执行器里）。

    :raises ServiceError: 下载器不可用（透传 :class:`PushError` 的状态码）
    """
    if not ids:
        raise ServiceError("未选择任何种子", status=400)

    # 下载器先解析一次（不存在/未配默认 → 整批 400，不逐条报）
    try:
        dl = await resolve_downloader(downloader_id)
    except PushError as exc:
        raise ServiceError(str(exc), status=exc.status) from exc
    dl_id = dl.id

    items: list[BatchPushItem] = []
    enqueued = skipped = failed = 0
    for index_id in ids:
        try:
            outcome = await enqueue_index_entry(index_id, dl_id)
        except PushError as exc:
            # 「这一条推不了」不算整批失败——分类记录后继续下一条
            if exc.code in ("not_found", "no_link", "site_missing"):
                skipped += 1
                items.append(
                    BatchPushItem(id=index_id, state=f"skipped_{exc.code}", detail=str(exc))
                )
            else:
                failed += 1
                items.append(
                    BatchPushItem(id=index_id, state="failed", detail=str(exc))
                )
            continue
        if outcome.state == "enqueued":
            enqueued += 1
            items.append(
                BatchPushItem(id=index_id, state="enqueued", detail=outcome.detail)
            )
        else:
            skipped += 1
            state = (
                "skipped_pushed"
                if outcome.state == "already_pushed"
                else "skipped_queued"
            )
            items.append(
                BatchPushItem(id=index_id, state=state, detail=outcome.detail)
            )

    return BatchPushResult(
        total=len(ids),
        enqueued=enqueued,
        skipped=skipped,
        failed=failed,
        items=items,
    )


async def push_entry(
    index_id: int, downloader_id: int | None = None
) -> PushOutcome:
    """把单条拉取索引推送到下载器。

    - 未指定 downloader_id → 用 is_default=True 且 enabled 的下载器
    - 已推送过（push_status == pushed）→ 直接返回已存在
    - 下载 .torrent 失败 → 502

    实现：走 :func:`app.services.push_service.push_index_entry`——
    与自动推送（``push_free``）**同一条队列 + 同一个执行器**
    （``task_queue`` → ``task_executor.execute_add_torrent``）。

    此前这里是独立实现，与自动推送链路分叉：手推不走队列（无崩溃恢复 /
    幂等键 / 退避），且两边的 TOCTOU 防御已经漂移。

    :raises ServiceError: 推送失败（状态码透传自 push_service）
    """
    try:
        return await push_index_entry(
            index_id,
            downloader_id,
            get_factory(),
            category="manual-push",
        )
    except PushError as exc:
        raise ServiceError(str(exc), status=exc.status) from exc
