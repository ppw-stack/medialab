"""拉取索引查询 API（FR-6.3）。

列出所有由 :func:`app.services.jobs.fetch_all_sites` 写入
:class:`~app.db.models.TorrentIndex` 的种子快照。

提供站点 / free_type / 关键字过滤与分页。详情窗口可读可推——后续手动
"按索引推送"功能可基于此 API 实现。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Query as QueryParam
from pydantic import BaseModel, field_serializer
from sqlalchemy import func, select

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.adapters.sites._bencode import infohash_from_torrent
from app.api.auth import require_session
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import (
    DownloaderInstance,
    PushStatus,
    Site,
    TorrentIndex,
)
from app.db.session import session_scope
from app.services.factory import get_factory

logger = get_logger(__name__)

router = APIRouter(
    prefix="/torrent-index",
    dependencies=[Depends(require_session)],
    tags=["torrent-index"],
)


class TorrentIndexItem(BaseModel):
    """单条拉取索引条目。"""

    id: int
    site_id: int
    site_name: str
    item_id: str | None
    name: str
    size: int
    category: str | None
    free_type: str
    free_end_at: datetime | None
    free_source: str | None
    free_confidence: float | None
    discount_pct: int | None
    #: 是否可推送（item_id 非空 → 可调 SiteAdapter.download_torrent 实时拿 bytes）
    pushable: bool
    push_status: str | None
    pushed_at: datetime | None
    info_hash: str | None
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime

    @field_serializer(
        "free_end_at", "pushed_at", "published_at", "first_seen_at", "last_seen_at",
        check_fields=False,
    )
    def _ser_dt(self, v: datetime | None) -> str | None:
        """naive datetime 视为 UTC，序列化为带 ``Z`` 的 ISO 8601。

        数据库统一以 UTC 存储（``app.core.timeutils.utcnow``），但 Pydantic 默认
        把 naive datetime 当作本地时间输出。显式追加 ``Z`` 让浏览器/客户端按 UTC
        解析并在本地时区显示——避免"7:30 拉取、15:25 发布"的视觉错乱。
        """
        if v is None:
            return None
        from datetime import UTC

        v = v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)
        return v.strftime("%Y-%m-%dT%H:%M:%S.") + f"{v.microsecond // 1000:03d}Z"


class TorrentIndexPage(BaseModel):
    """分页响应。"""

    total: int
    items: list[TorrentIndexItem]


#: 允许排序的列白名单（防止任意属性访问）
_SORTABLE_COLUMNS: Final[dict[str, Any]] = {
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


@router.get("", response_model=TorrentIndexPage)
async def list_torrent_index(
    site_id: int | None = QueryParam(default=None, description="按站点过滤"),
    free_type: str | None = QueryParam(default=None, description="按 free_type 过滤"),
    q: str | None = QueryParam(default=None, description="种子名关键字"),
    sort_by: str = QueryParam(default="last_seen_at", description="排序字段"),
    order: str = QueryParam(default="desc", description="asc | desc"),
    offset: int = QueryParam(default=0, ge=0),
    limit: int = QueryParam(default=50, ge=1, le=200),
) -> TorrentIndexPage:
    """返回拉取索引分页列表。默认按 ``last_seen_at DESC`` 排序。"""
    stmt = select(TorrentIndex, Site.name).join(Site, Site.id == TorrentIndex.site_id)

    if site_id is not None:
        stmt = stmt.where(TorrentIndex.site_id == site_id)
    if free_type:
        stmt = stmt.where(TorrentIndex.free_type == free_type)
    if q:
        stmt = stmt.where(TorrentIndex.name.contains(q))

    # 排序：sort_by 必须在白名单内；order 只能是 asc/desc
    sort_col = _SORTABLE_COLUMNS.get(sort_by, TorrentIndex.last_seen_at)
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
            await session.execute(
                stmt.offset(offset).limit(limit)
            )
        ).all()

    items = [
        TorrentIndexItem(
            id=row.TorrentIndex.id,
            site_id=row.TorrentIndex.site_id,
            site_name=row.name,
            item_id=row.TorrentIndex.item_id,
            name=row.TorrentIndex.name,
            size=row.TorrentIndex.size,
            category=row.TorrentIndex.category,
            free_type=row.TorrentIndex.free_type,
            free_end_at=row.TorrentIndex.free_end_at,
            free_source=row.TorrentIndex.free_source,
            free_confidence=row.TorrentIndex.free_confidence,
            discount_pct=row.TorrentIndex.discount_pct,
            pushable=row.TorrentIndex.item_id is not None,
            push_status=row.TorrentIndex.push_status,
            pushed_at=row.TorrentIndex.pushed_at,
            info_hash=row.TorrentIndex.info_hash,
            published_at=row.TorrentIndex.published_at,
            first_seen_at=row.TorrentIndex.first_seen_at,
            last_seen_at=row.TorrentIndex.last_seen_at,
        )
        for row in rows
    ]
    return TorrentIndexPage(total=int(total), items=items)


@router.get("/sites", response_model=list[dict[str, int | str]])
async def list_sites() -> list[dict[str, int | str]]:
    """返回站点列表供前端过滤下拉。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Site.id, Site.name).order_by(Site.id.asc())
            )
        ).all()
    return [{"id": r[0], "name": r[1]} for r in rows]


@router.get("/free-types", response_model=list[str])
async def list_free_types() -> list[str]:
    """返回当前存在的 free_type 取值列表，供前端过滤下拉。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TorrentIndex.free_type)
                .distinct()
                .order_by(TorrentIndex.free_type.asc())
            )
        ).all()
    return [r[0] for r in rows if r[0]]


class TorrentIndexStats(BaseModel):
    """拉取索引统计：总条数 + 按 free_type 分布。"""

    total: int
    by_type: dict[str, int]


@router.get("/stats", response_model=TorrentIndexStats)
async def get_torrent_index_stats(
    site_id: int | None = QueryParam(default=None, description="按站点过滤"),
) -> TorrentIndexStats:
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
    return TorrentIndexStats(total=int(total), by_type=by_type)


@router.get("/{index_id}", response_model=TorrentIndexItem)
async def get_torrent_index(index_id: int) -> TorrentIndexItem:
    """返回单条拉取索引详情。"""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(TorrentIndex, Site.name)
                .join(Site, Site.id == TorrentIndex.site_id)
                .where(TorrentIndex.id == index_id)
            )
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="torrent_index entry not found")
    return TorrentIndexItem(
        id=row.TorrentIndex.id,
        site_id=row.TorrentIndex.site_id,
        site_name=row.name,
        item_id=row.TorrentIndex.item_id,
        name=row.TorrentIndex.name,
        size=row.TorrentIndex.size,
        category=row.TorrentIndex.category,
        free_type=row.TorrentIndex.free_type,
        free_end_at=row.TorrentIndex.free_end_at,
        free_source=row.TorrentIndex.free_source,
        free_confidence=row.TorrentIndex.free_confidence,
        discount_pct=row.TorrentIndex.discount_pct,
        pushable=row.TorrentIndex.item_id is not None,
        push_status=row.TorrentIndex.push_status,
        pushed_at=row.TorrentIndex.pushed_at,
        info_hash=row.TorrentIndex.info_hash,
        published_at=row.TorrentIndex.published_at,
        first_seen_at=row.TorrentIndex.first_seen_at,
        last_seen_at=row.TorrentIndex.last_seen_at,
    )


class PushRequest(BaseModel):
    """推送请求。downloader_id 省略时用默认下载器。"""

    downloader_id: int | None = None


class PushResponse(BaseModel):
    """推送结果。"""

    ok: bool
    detail: str = ""
    info_hash: str | None = None
    downloader_id: int | None = None


@router.post("/{index_id}/push", response_model=PushResponse)
async def push_torrent_index(index_id: int, payload: PushRequest) -> PushResponse:
    """把单条拉取索引推送到下载器。

    - 未指定 downloader_id → 用 is_default=True 且 enabled 的下载器
    - 已推送过（push_status == pushed）→ 直接返回已存在
    - 实时调 SiteAdapter.download_torrent 拿 .torrent bytes；下载失败 → 400
    """
    from sqlalchemy import update

    from app.db.models import Site

    # 1) 读索引条目 + 站点（用于构造 adapter）
    async with session_scope() as session:
        item = (
            await session.execute(select(TorrentIndex).where(TorrentIndex.id == index_id))
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="索引条目不存在")
        if not item.item_id:
            raise HTTPException(status_code=400, detail="该条目缺少 item_id，无法推送")
        site_row = (
            await session.execute(
                select(Site).where(Site.id == item.site_id)
            )
        ).scalar_one_or_none()
        if site_row is None:
            raise HTTPException(status_code=404, detail="站点不存在")

        # 2) 解析下载器
        if payload.downloader_id is not None:
            dl = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.id == payload.downloader_id
                    )
                )
            ).scalar_one_or_none()
            if dl is None:
                raise HTTPException(status_code=404, detail="下载器不存在")
            if not dl.enabled:
                raise HTTPException(status_code=400, detail="下载器已禁用")
        else:
            dl = (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.is_default.is_(True),
                        DownloaderInstance.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if dl is None:
                raise HTTPException(status_code=400, detail="未配置默认下载器")

        # 3) 去重：push_status == pushed 则直接返回
        if item.push_status == PushStatus.PUSHED:
            return PushResponse(
                ok=True, detail="该种已推送过", downloader_id=dl.id
            )

        item_id = item.item_id
        name = item.name
        dl_id = dl.id
        dl_row = dl

    # 4) 下载 .torrent 文件
    factory = get_factory()
    site_adapter = factory.site(site_row)
    try:
        torrent_bytes = await site_adapter.download_torrent(item_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail=f"下载 torrent 文件失败：{exc}"
        ) from exc
    if not torrent_bytes or not torrent_bytes.startswith(b"d"):
        raise HTTPException(
            status_code=400, detail="站点返回的不是有效的 torrent 文件"
        )

    # 5) 解析 info_hash
    info_hash = infohash_from_torrent(torrent_bytes)
    if info_hash is None:
        raise HTTPException(
            status_code=400, detail="torrent 文件解析 info_hash 失败"
        )

    # 6) 调下载器
    adapter = factory.downloader(dl_row)
    try:
        await adapter.add_torrent(
            AddTorrentPayload(
                name=name,
                torrent_bytes=torrent_bytes,
                category="manual-push",
                save_path=dl_row.save_path or None,
            )
        )
    except DownloaderError as exc:
        raise HTTPException(status_code=502, detail=f"下载器推送失败：{exc}") from exc

    # 7) 更新 torrent_index.push_status = pushed
    async with session_scope() as session:
        await session.execute(
            update(TorrentIndex)
            .where(TorrentIndex.id == index_id)
            .values(
                push_status=PushStatus.PUSHED,
                pushed_at=utcnow(),
                info_hash=info_hash,
            )
        )

    logger.info("手动推送 index_id=%d → downloader=%d item=%s", index_id, dl_id, item_id)
    return PushResponse(ok=True, detail="推送成功", info_hash=info_hash, downloader_id=dl_id)