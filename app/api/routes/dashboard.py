"""总览仪表盘 API（FR-6.1）。

站点/下载器启用情况 + 免费窗口倒计时 + 索引规模 + 下载器在途任务。

**降频**：``inflight_torrents`` 需跨网络调下载器全量种子列表，而前端每 5 秒
轮询一次，故缓存 30 秒（实际请求量降到 1/6）。纯 DB 查询的字段不做缓存。

**降级**：任一子查询失败 → 该字段返回 ``None``（前端显示 ``—``），
不影响其他字段，接口不返回 5xx。

统计口径全在 :mod:`app.services.dashboard_service`，路由只做编排。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.auth import require_session
from app.services import dashboard_service

router = APIRouter(dependencies=[Depends(require_session)], tags=["dashboard"])


@router.get("/dashboard", summary="总览概览")
async def dashboard() -> dict[str, Any]:
    """站点/下载器启用情况 + 免费窗口倒计时 + 索引规模 + 在途任务。"""
    site_total, site_enabled, dl_total, dl_enabled = (
        await dashboard_service.site_and_downloader_counts()
    )

    free_ending_soon = await dashboard_service.count_free_ending_soon()
    free_index_total = await dashboard_service.count_index_total()
    inflight_torrents = await dashboard_service.count_inflight_torrents()
    inflight_freshness = await dashboard_service.snapshot_freshness()

    return {
        "sites": {"total": site_total, "enabled": site_enabled},
        "downloaders": {"total": dl_total, "enabled": dl_enabled},
        "free_ending_soon": free_ending_soon,
        "free_index_total": free_index_total,
        "inflight_torrents": inflight_torrents,
        # 在途数据的抓取时刻（辅种部分来自定时快照）——前端据此显示
        # "数据来自 X 分钟前"，避免用户以为是实时的。
        "inflight_snapshot_at": inflight_freshness,
    }
