"""拉取索引查询 API（FR-6.3）。

列出所有由 :func:`app.services.jobs.fetch_all_sites` 写入
:class:`~app.db.models.TorrentIndex` 的种子快照。

提供站点 / free_type / 关键字过滤与分页。详情窗口可读可推。

查询与推送逻辑在 :mod:`app.services.index_service`，路由只做参数解析
与结果转换。
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Response
from fastapi import Query as QueryParam
from pydantic import BaseModel, Field

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services import index_service

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


class TorrentIndexPage(BaseModel):
    """分页响应。"""

    total: int
    items: list[TorrentIndexItem]


class TorrentIndexStats(BaseModel):
    """拉取索引统计：总条数 + 按 free_type 分布。"""

    total: int
    by_type: dict[str, int]


class PushRequest(BaseModel):
    """推送请求。downloader_id 省略时用默认下载器。"""

    downloader_id: int | None = None


class PushResponse(BaseModel):
    """推送结果。"""

    ok: bool
    detail: str = ""
    info_hash: str | None = None
    downloader_id: int | None = None
    #: True = 已入队、后台自动重试，尚未完成（HTTP 202）。
    #:
    #: 下载失败属于可重试类（站点瞬时异常）——不能报「失败」，
    #: 否则用户会以为种子丢了（2026-11-21 prod 实测：报 502 后 5 分钟补推成功）。
    deferred: bool = False


class BatchPushRequest(BaseModel):
    """批量推送请求。

    ``ids`` **不限数量**——一次勾选几百条是常见操作。
    """

    ids: list[int] = Field(..., min_length=1, description="拉取索引 id 列表")
    downloader_id: int | None = None


class BatchPushItemOut(BaseModel):
    """批量推送的单条结果。"""

    id: int
    state: str
    detail: str = ""


class BatchPushResponse(BaseModel):
    """批量推送汇总。

    **只入队不同步执行**：执行由 ``drain_task_queue`` 按队列节流消化
    （每轮 50 条、条间 2s），执行时自动更新 ``push_status``。
    """

    total: int
    enqueued: int
    skipped: int
    failed: int
    items: list[BatchPushItemOut]


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
    page = await index_service.list_index(
        site_id=site_id,
        free_type=free_type,
        q=q,
        sort_by=sort_by,
        order=order,
        offset=offset,
        limit=limit,
    )
    return TorrentIndexPage(
        total=page.total,
        items=[TorrentIndexItem(**asdict(i)) for i in page.items],
    )


@router.get("/sites", response_model=list[dict[str, int | str]])
async def list_sites() -> list[dict[str, int | str]]:
    """返回站点列表供前端过滤下拉。"""
    return [
        {"id": s.id, "name": s.name}
        for s in await index_service.list_index_sites()
    ]


@router.get("/free-types", response_model=list[str])
async def list_free_types() -> list[str]:
    """返回当前存在的 free_type 取值列表，供前端过滤下拉。"""
    return await index_service.list_free_types()


@router.get("/stats", response_model=TorrentIndexStats)
async def get_torrent_index_stats(
    site_id: int | None = QueryParam(default=None, description="按站点过滤"),
) -> TorrentIndexStats:
    """总条数 + 按 free_type 分布，供前端 stat 卡片使用。

    注意：与列表页 query（关键字 `q`）解耦——统计反映库内全量，避免
    卡片数字与列表筛选相互干扰。
    """
    stats = await index_service.index_stats(site_id=site_id)
    return TorrentIndexStats(total=stats.total, by_type=stats.by_type)


@router.post("/push-batch", response_model=BatchPushResponse)
async def push_torrent_index_batch(payload: BatchPushRequest) -> BatchPushResponse:
    """把**多条**拉取索引推送到下载器（批量手动推送）。

    - 每条走与单推**同一份**校验 / 幂等 / 入队逻辑
      （:func:`app.services.index_service.push_batch`）
    - **只入队，不同步执行**：几百条同步执行会把 HTTP 请求挂十几分钟；
      入队后由 ``drain_task_queue`` 按队列节流消化（每轮 50 条、条间 2s），
      执行时自动更新 ``push_status``
    - 单条推不了（已推过 / 队列里已有 / 无可用链接）→ 计入 ``skipped``，
      **不影响其它条目**
    """
    result = await index_service.push_batch(payload.ids, payload.downloader_id)

    logger.info(
        "批量推送：共 %d 条 → 入队 %d / 跳过 %d / 失败 %d",
        result.total, result.enqueued, result.skipped, result.failed,
    )
    return BatchPushResponse(
        total=result.total,
        enqueued=result.enqueued,
        skipped=result.skipped,
        failed=result.failed,
        items=[
            BatchPushItemOut(id=i.id, state=i.state, detail=i.detail)
            for i in result.items
        ],
    )


@router.get("/{index_id}", response_model=TorrentIndexItem)
async def get_torrent_index(index_id: int) -> TorrentIndexItem:
    """返回单条拉取索引详情。"""
    entry = await index_service.get_index_entry(index_id)
    return TorrentIndexItem(**asdict(entry))


@router.post("/{index_id}/push", response_model=PushResponse)
async def push_torrent_index(
    index_id: int, payload: PushRequest, response: Response
) -> PushResponse:
    """把单条拉取索引推送到下载器。

    - 未指定 downloader_id → 用 is_default=True 且 enabled 的下载器
    - 已推送过（push_status == pushed）→ 直接返回已存在
    - **下载 .torrent 失败（可重试类）→ 202 + ``deferred=true``**：
      任务已入队，``drain_task_queue`` 会按指数退避自动重试（最多 5 次）。
      报 502 会让用户以为种子丢了，而实测多数会在几分钟内补推成功。
    - **永久失败 / 重试已用尽 → 502**：站点凭据失效、详情页里没有下载地址
      （重试也不会成功），或队列里的重试预算已耗尽（不会自动再跑，
      需重新点击推送重置死信任务）
    - 其它失败（站点停用 / 索引不存在 / 解析失败）→ 4xx / 502

    实现：走 :func:`app.services.push_service.push_index_entry`——
    与自动推送（``push_free``）**同一条队列 + 同一个执行器**
    （``task_queue`` → ``task_executor.execute_add_torrent``）。

    此前这里是独立实现，与自动推送链路分叉：手推不走队列（无崩溃恢复 /
    幂等键 / 退避），且两边的 TOCTOU 防御已经漂移。
    """
    outcome = await index_service.push_entry(index_id, payload.downloader_id)

    if outcome.deferred:
        # 202 Accepted：请求已受理，执行在后台队列继续
        response.status_code = 202
    logger.info(
        "手动推送 index_id=%d → downloader=%s%s：%s",
        index_id,
        outcome.downloader_id,
        "（已入队重试）" if outcome.deferred else "",
        outcome.detail,
    )
    return PushResponse(
        ok=outcome.ok,
        detail=outcome.detail,
        info_hash=outcome.info_hash,
        downloader_id=outcome.downloader_id,
        deferred=outcome.deferred,
    )
