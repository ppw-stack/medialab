"""总览仪表盘 API（FR-6.1）。

站点/下载器启用情况 + 免费窗口倒计时 + 索引规模 + 下载器在途任务。

**降频**：``inflight_torrents`` 需跨网络调下载器全量种子列表，而前端每 5 秒
轮询一次，故用 :class:`app.core.simple_cache.TTLCache` 缓存 30 秒
（实际请求量降到 1/6）。纯 DB 查询的字段不做缓存。

**降级**：任一子查询失败 → 该字段返回 ``None``（前端显示 ``—``），
不影响其他字段，接口不返回 5xx。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.adapters.downloaders.base import TorrentState
from app.api.auth import require_session
from app.core.logging import get_logger
from app.core.simple_cache import TTLCache
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_session)], tags=["dashboard"])

#: 免费窗口"即将截止"的判定阈值（小时）
FREE_ENDING_SOON_HOURS = 6

#: 下载器在途任务数的缓存 TTL（秒）——前端 5s 轮询，30s TTL 足够新鲜
_INFLIGHT_TTL_SECONDS = 30.0

#: 进程内缓存（模块级单例；多进程部署时各进程各持一份，是刻意的）
_inflight_cache: TTLCache[int | None] = TTLCache(ttl_seconds=_INFLIGHT_TTL_SECONDS)


@router.get("/dashboard", summary="总览概览")
async def dashboard() -> dict[str, Any]:
    """站点/下载器启用情况 + 免费窗口倒计时 + 索引规模 + 在途任务。"""
    site_total, site_enabled, dl_total, dl_enabled = await _site_and_downloader_counts()

    free_ending_soon = await _count_free_ending_soon()
    free_index_total = await _count_index_total()
    inflight_torrents = await _count_inflight_torrents()

    return {
        "sites": {"total": site_total, "enabled": site_enabled},
        "downloaders": {"total": dl_total, "enabled": dl_enabled},
        "free_ending_soon": free_ending_soon,
        "free_index_total": free_index_total,
        "inflight_torrents": inflight_torrents,
    }


async def _site_and_downloader_counts() -> tuple[int, int, int, int]:
    """站点/下载器的总数与启用数。失败返回全 0（不抛异常）。"""
    try:
        async with session_scope() as session:
            site_total = (
                await session.execute(select(func.count(Site.id)))
            ).scalar_one()
            site_enabled = (
                await session.execute(
                    select(func.count(Site.id)).where(Site.enabled.is_(True))
                )
            ).scalar_one()
            dl_total = (
                await session.execute(select(func.count(DownloaderInstance.id)))
            ).scalar_one()
            dl_enabled = (
                await session.execute(
                    select(func.count(DownloaderInstance.id)).where(
                        DownloaderInstance.enabled.is_(True)
                    )
                )
            ).scalar_one()
        return site_total, site_enabled, dl_total, dl_enabled
    except Exception as exc:
        logger.warning("仪表盘统计（站点/下载器）查询失败：%s", exc)
        return 0, 0, 0, 0


async def _count_free_ending_soon() -> int | None:
    """免费窗口 ≤ 6 小时内截止的种子数。

    ``free_end_at`` 是北京时区字符串（``yyyy-MM-dd HH:mm:ss``），字典序
    等于时间序，故直接用字符串比较——不引入时区转换风险。
    """
    try:
        now = format_bj(bj_now_dt())
        deadline = format_bj(bj_now_dt() + timedelta(hours=FREE_ENDING_SOON_HOURS))
        async with session_scope() as session:
            count = (
                await session.execute(
                    select(func.count(TorrentIndex.id)).where(
                        TorrentIndex.free_end_at.is_not(None),
                        TorrentIndex.free_end_at > now,
                        TorrentIndex.free_end_at <= deadline,
                        TorrentIndex.free_type.in_(
                            (FreeType.TIMED, FreeType.TWOU_TIMED)
                        ),
                    )
                )
            ).scalar_one()
        return int(count)
    except Exception as exc:
        logger.warning("仪表盘统计（即将截止）查询失败：%s", exc)
        return None


async def _count_index_total() -> int | None:
    """torrent_index 索引总条数。"""
    try:
        async with session_scope() as session:
            count = (
                await session.execute(select(func.count(TorrentIndex.id)))
            ).scalar_one()
        return int(count)
    except Exception as exc:
        logger.warning("仪表盘统计（索引总数）查询失败：%s", exc)
        return None


async def _count_inflight_torrents() -> int | None:
    """下载器在途任务合计（跨所有启用的下载器）。

    "在途"= 正在参与的任务：做种 / 下载 / 检查 / 停滞——**排除**已暂停与
    致命错误（error / missingFiles）的任务。

    刻意不排除"已完成"：PT 辅种场景下做种（seeding）正是主要活跃状态，
    排除它会让该指标恒为 0。

    任一下载器不可达 → 整体返回 ``None``（前端显示 ``—``），避免用不完整
    的数据误导用户。
    """
    try:
        return await _inflight_cache.get_or_set(_load_inflight_torrents)
    except Exception as exc:
        logger.warning("仪表盘统计（在途任务）查询失败：%s", exc)
        return None


async def _load_inflight_torrents() -> int | None:
    """实际拉取各下载器任务并计数（缓存 loader）。"""
    from app.services.factory import get_factory

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.enabled.is_(True)
                    )
                )
            ).scalars().all()
        )

    if not rows:
        return 0

    factory = get_factory()
    total = 0
    for row in rows:
        try:
            # 复用工厂缓存的适配器实例（连接池复用，见 factory 模块说明）
            adapter = factory.downloader(row)
            infos = await adapter.list_torrents()
        except Exception as exc:
            logger.warning(
                "仪表盘统计：下载器 %s 不可达（%s）", row.name, exc
            )
            return None
        total += sum(1 for t in infos if _is_inflight(t))
    return total


def _is_inflight(info: Any) -> bool:
    """是否为"在途"任务：非暂停、非致命错误。"""
    state = info.state
    if not isinstance(state, TorrentState):
        return False
    return not (state.is_paused or state.is_fatal)
