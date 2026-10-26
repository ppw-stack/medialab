"""辅种下载器巡查 API.

端点 (prefix=/api/v1/cross-seed-monitor):
- GET  /downloaders                       列出 is_cross_seed=True 下载器
- GET  /snapshots                         辅种下载器快照列表（DB 读，毫秒级）
- GET  /downloaders/{id}/snapshot         单个辅种下载器快照详情
- POST /downloaders/{id}/refresh          手动刷新单个辅种下载器（抓取+落库）
- GET  /downloaders/{id}/scan             实时扫描单个辅种下载器（调试用）

巡查本身是调度作业 ``scan_cross_seed``（cron ``2,32 * * * *``），由 cron
定时抓取并把结果写入 ``cross_seed_snapshot`` 表，异常种子自动暂停。

**前端读快照，不打实时接口**：辅种下载器的种子量大（实测 3000+ 个），
实时调下载器接口要 20s+。实时扫描端点 ``/scan`` 保留作调试 / 应急用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services.cross_seed_monitor import (
    CrossSeedMonitorService,
    DownloaderDisabled,
    DownloaderError,
    DownloaderNotFound,
    NotCrossSeed,
    UnsupportedDownloader,
)
from app.services.cross_seed_space import SpaceStats, summarize_spaces
from app.services.downloader_service import remove_torrents_in_batches
from app.services.errors import ServiceError

logger = get_logger(__name__)

router = APIRouter(
    prefix="/cross-seed-monitor",
    dependencies=[Depends(require_session)],
    tags=["cross-seed-monitor"],
)


# ============================================================ DTO
class DownloaderItem(BaseModel):
    id: int
    name: str
    type: str
    url: str
    enabled: bool
    is_cross_seed: bool


class SnapshotHeaderOut(BaseModel):
    """快照头部（列表视图）。"""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    total_torrents: int
    unique_count: int
    anomaly_count: int
    inflight_count: int
    #: 本次**实际**发出暂停指令的种子数（不含已暂停被跳过的）
    paused_count: int = 0
    #: 暂停失败的种子数
    pause_failed_count: int = 0
    #: 磁盘剩余空间（字节）；None = 没取到
    disk_free_bytes: int | None = None
    #: 磁盘总量（字节）；None = 该下载器不提供（qBittorrent）
    disk_total_bytes: int | None = None
    #: 种子声明占用（字节）——各种子 ``completed_bytes`` 求和。
    #: ⚠️ 含**共享数据重复计入**，不等于磁盘真实占用。
    used_bytes: int = 0
    #: 种子总大小（字节，理论值）——同样含共享重复计入。
    total_size_bytes: int = 0


class SnapshotListOut(BaseModel):
    """快照列表 + 未抓取过的辅种下载器.

    ``pending`` 列出「是辅种但还没有快照行」的下载器——前端据此显示
    「等待首次抓取」而不是把空数据渲染成「没有种子」。
    """

    snapshots: list[SnapshotHeaderOut]
    pending: list[DownloaderItem]


class RefreshResultOut(BaseModel):
    """手动刷新的结果（与快照详情同结构）。"""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    total_torrents: int
    unique_count: int
    anomaly_count: int
    inflight_count: int
    paused_count: int = 0
    pause_failed_count: int = 0
    #: 磁盘剩余空间（字节）；None = 没取到
    disk_free_bytes: int | None = None
    #: 磁盘总量（字节）；None = 该下载器不提供（qBittorrent）
    disk_total_bytes: int | None = None
    #: 种子声明占用（字节）——各种子 ``completed_bytes`` 求和。
    #: ⚠️ 含**共享数据重复计入**，不等于磁盘真实占用。
    used_bytes: int = 0
    #: 种子总大小（字节，理论值）——同样含共享重复计入。
    total_size_bytes: int = 0
    #: 按下载目录的声明占用分布
    by_dir: list[dict[str, Any]] = []
    stats: list[dict[str, Any]]
    anomalies: list[dict[str, Any]]


class RemoveTorrentsIn(BaseModel):
    """批量删除种子的请求体。"""

    #: 要删除的种子 infohash 列表（40 位 hex，由 service 层校验）
    hashes: list[str] = Field(..., min_length=1)
    #: 是否连磁盘数据一起删（**不可恢复**）
    delete_data: bool = False


class DeleteOrphansIn(BaseModel):
    """删除孤儿文件的请求体。"""

    #: 要删除的孤儿条目的**绝对路径**（来自孤儿明细的 path 字段）
    paths: list[str] = Field(..., min_length=1)


# ============================================================ 端点
@router.get("/downloaders", summary="辅种下载器列表")
async def list_downloaders() -> list[DownloaderItem]:
    """返回 is_cross_seed=True 且 enabled=True 的下载器."""
    svc = CrossSeedMonitorService()
    rows = await svc.list_cross_seed_downloaders()
    return [
        DownloaderItem(
            id=r.id,
            name=r.name,
            type=r.type,
            url=r.url,
            enabled=bool(r.enabled),
            is_cross_seed=bool(r.is_cross_seed),
        )
        for r in rows
    ]


@router.get("/snapshots", summary="辅种下载器快照列表")
async def list_snapshots() -> SnapshotListOut:
    """返回所有辅种下载器的快照头部 + 尚未抓取过的下载器.

    **纯 DB 读**（毫秒级）——不碰下载器接口。
    """
    svc = CrossSeedMonitorService()
    rows = await svc.list_cross_seed_downloaders()
    headers = await svc.list_snapshot_headers()
    have = {h.downloader_id for h in headers}
    return SnapshotListOut(
        snapshots=[SnapshotHeaderOut(**h.to_dict()) for h in headers],
        pending=[
            DownloaderItem(
                id=r.id,
                name=r.name,
                type=r.type,
                url=r.url,
                enabled=bool(r.enabled),
                is_cross_seed=bool(r.is_cross_seed),
            )
            for r in rows
            if r.id not in have
        ],
    )


@router.get("/space", summary="辅种空间汇总")
async def list_space() -> dict[str, Any]:
    """所有辅种下载器的空间汇总（磁盘剩余 / 辅种占用）。

    **纯 DB 读**（毫秒级）——数据来自定时作业写入的快照。

    只统计 ``is_cross_seed=True`` 的下载器（非辅种的普通下载器不占辅种
    空间，不在此列）。
    """
    svc = CrossSeedMonitorService()
    headers = await svc.list_snapshot_headers()
    summary = summarize_spaces([
        (
            h.downloader_name,
            SpaceStats(
                used_bytes=h.used_bytes,
                total_size_bytes=h.total_size_bytes,
                disk_free_bytes=h.disk_free_bytes,
                disk_total_bytes=h.disk_total_bytes,
            ),
        )
        for h in headers
    ])
    return {
        "summary": summary.to_dict(),
        "downloaders": [
            {
                "downloader_id": h.downloader_id,
                "downloader_name": h.downloader_name,
                "scanned_at": h.scanned_at,
                "used_bytes": h.used_bytes,
                "total_size_bytes": h.total_size_bytes,
                "disk_free_bytes": h.disk_free_bytes,
                "disk_total_bytes": h.disk_total_bytes,
            }
            for h in headers
        ],
    }


@router.post("/downloaders/{downloader_id}/remove-torrents", summary="批量删除种子")
async def remove_torrents(
    downloader_id: int, payload: RemoveTorrentsIn
) -> dict[str, Any]:
    """删除下载器里的种子（**通用**——不限是异常还是正常种子）。

    两处调用：
    - 异常种子明细（tracker 未注册 / 数据丢失）
    - 统计卡片的分组（同名同大小的一组种子）

    **不限数量**（分批发送）——辅种站的异常种子动辄数百个，超出普通批量
    操作上限（200）时会分批逐个删。

    :param delete_data: True = 连**磁盘数据**一起删（不可恢复，见
        :func:`app.services.downloader_service.remove_torrents_in_batches`
        的审计日志）。

    删除后建议重新抓取一次快照，让 UI 反映最新状态。
    """
    try:
        result = await remove_torrents_in_batches(
            downloader_id,
            payload.hashes,
            delete_data=payload.delete_data,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {
        "requested": result.requested,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "errors": result.errors,
    }


@router.get("/orphans", summary="辅种孤儿文件统计")
async def list_orphans() -> dict[str, Any]:
    """所有辅种下载器的孤儿文件统计（文件在、任务没了）。

    **纯 DB 读**——数据来自定时作业 ``scan_orphans`` 写入的
    ``cross_seed_orphan`` 表。

    没有记录 = 还没扫过（或该环境没挂载辅种目录）。
    """
    return await _orphan_summary_payload()


@router.post("/orphans/scan", summary="手动扫描孤儿文件")
async def scan_orphan_files() -> dict[str, Any]:
    """立即扫描孤儿文件（不等 cron）。

    孤儿扫描默认**每天一次**（cron \`7 4 * * *\`）——用户当天发现一个孤儿
    想现在清理，等不到明天。本端点提供「只扫不删」的手动入口。

    **同步 await**（实测 40–60 s，扫盘 + 拉所有下载器 + 落库）——与辅种页
    的「立即抓取」同一性质（前端要等）。

    返回体带上有孤儿表的新鲜统计，前端直接用，不用再发一次请求。
    """
    from app.services.cross_seed_orphan import run_scan_orphans

    result = await run_scan_orphans()
    out: dict[str, Any] = {
        "summary": result.get("summary", ""),
        "orphan_count": result.get("orphan_count", 0),
        "orphan_bytes": result.get("orphan_bytes", 0),
        "errors": result.get("errors", []),
        "orphans": await _orphan_summary_payload(),
    }
    return out


@router.post("/orphans/delete", summary="删除孤儿文件（不可逆）")
async def delete_orphan_files(payload: DeleteOrphansIn) -> dict[str, Any]:
    """删除孤儿文件（磁盘上有、下载器里已无对应任务的条目）。

    **不可逆**——删的是 NAS 上的实际数据。安全机制在 service 层
    （:mod:`app.services.orphan_deleter`），不只靠前端弹框：

    1. **只在配置的扫描目录内删**——路径必须是扫描目录的**直接子项**，
       不能是任意路径（防路径穿越）
    2. **二次扫描复核**——删除前**重新**拉一遍所有下载器的任务，确认这些
       条目**此刻**仍无主（快照可能是几小时前的，期间情况可能变了）
    3. **审计日志**——逐条记路径 + 大小

    逐条删除，单条失败不中断其余条目。复核不通过的会被**跳过**
    （记在 ``skipped`` 里），不会删。

    **删除后自动重扫**：孤儿统计表是**每天一次**的作业快照，删完不重扫，
    页面会一直显示旧数字（已删的还在列表里、且看不到新产生的孤儿）。
    故删除后**立即重扫**并把最新统计带在响应里（``orphans`` 字段），
    前端据此刷新，不用再等下一个 cron。

    重扫失败**不影响删除结果**（数据已删，只是统计没更新）——记
    ``rescan_error``，前端提示用户可稍后手动重扫。
    """
    from app.services.cross_seed_orphan import (
        collect_owned_names,
        run_scan_orphans,
    )
    from app.services.env_settings import get_env_setting
    from app.services.orphan_deleter import delete_orphans
    from app.services.orphan_scanner import parse_scan_dirs

    scan_dirs = parse_scan_dirs(get_env_setting("cross_seed_scan_dirs"))
    if not scan_dirs:
        raise HTTPException(
            status_code=400, detail="未配置扫描目录（辅种下载目录未挂载到容器）"
        )

    owned, _by_downloader, pull_failed = await collect_owned_names()
    result = delete_orphans(payload.paths, owned, scan_dirs)

    out = result.to_dict()
    if pull_failed:
        # 拉取失败的下载器不贡献「有主」→ 判定可能不完整 → 必须点名
        out["warning"] = (
            f"以下下载器拉取失败，判定可能不完整：{'、'.join(pull_failed)}"
        )

    # 删除后重扫：让页面立刻反映最新状态（已删的不再出现、
    # 新产生的孤儿也能看到）。失败不影响删除结果。
    try:
        await run_scan_orphans()
        out["rescan_error"] = None
    except Exception as exc:  # 重扫失败不该掩盖删除结果（数据已删）
        logger.warning("删除后重扫孤儿统计失败：%s", exc)
        out["rescan_error"] = str(exc) or type(exc).__name__

    # 带上有孤儿表的新鲜统计（前端直接用，不用再发一次请求）
    out["orphans"] = await _orphan_summary_payload()
    return out


async def _orphan_summary_payload() -> dict[str, Any]:
    """孤儿统计的汇总载荷（与 ``GET /orphans`` 同结构）。"""
    from app.services.cross_seed_orphan import list_orphans as _list

    rows = await _list()
    return {
        "summary": {
            "downloaders": len(rows),
            "orphan_count": sum(r.orphan_count for r in rows),
            "orphan_bytes": sum(r.orphan_bytes for r in rows),
            "empty_dir_count": sum(r.empty_dir_count for r in rows),
        },
        "downloaders": [r.to_dict() for r in rows],
    }


@router.get("/downloaders/{downloader_id}/snapshot", summary="单个辅种下载器快照")
async def get_snapshot(downloader_id: int) -> dict[str, Any]:
    """取单个辅种下载器的快照详情（含分组统计 + 异常明细）.

    还没有快照 → 404（前端据此显示「等待首次抓取」）。
    """
    svc = CrossSeedMonitorService()
    snap = await svc.get_snapshot(downloader_id)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"下载器 {downloader_id} 还没有快照（尚未抓取）",
        )
    return snap


@router.post("/downloaders/{downloader_id}/refresh", summary="手动刷新辅种下载器")
async def refresh_downloader(downloader_id: int) -> RefreshResultOut:
    """立即抓取单个辅种下载器 → 落库快照 → 暂停异常种子.

    **await 完整抓取**（辅种站 20s+）——反向代理超时要 > 60s。
    """
    svc = CrossSeedMonitorService()
    try:
        result = await svc.refresh_downloader(downloader_id)
    except DownloaderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotCrossSeed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DownloaderDisabled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedDownloader as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except DownloaderError as exc:
        # 下载器可达但报错（认证失败 / 网络异常 / 协议错误）——返回 502
        # 结构化错误，而不是漏成 500 带堆栈。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RefreshResultOut(**result.to_dict())


@router.get("/downloaders/{downloader_id}/scan", summary="实时扫描单个辅种下载器")
async def scan_one(downloader_id: int) -> dict[str, Any]:
    """实时拉 tr, 返回异常 + 统计 + 扫描时间. **不落库**.

    调试 / 应急用——前端主路径读快照（``/snapshots``）。
    """
    svc = CrossSeedMonitorService()
    try:
        result = await svc.scan_one(downloader_id)
    except DownloaderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NotCrossSeed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DownloaderDisabled as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedDownloader as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except DownloaderError as exc:
        # 下载器可达但报错（认证失败 / 网络异常 / 协议错误）——返回 502
        # 结构化错误，而不是漏成 500 带堆栈。
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return result.to_dict()
