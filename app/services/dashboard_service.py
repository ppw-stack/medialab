"""仪表盘统计服务（FR-6.1）。

此前 :mod:`app.api.routes.dashboard` 的路由层直接写 SQL（5 处
``session_scope``）并**直接调下载器适配器**拉全量种子列表。路由层同时
碰 DB 与网络，既无法单测，也让「仪表盘」这个纯读接口承担了跨网络
IO 的编排责任。

本模块把统计口径下沉到服务层：

- :func:`site_and_downloader_counts` —— 站点/下载器总数与启用数
- :func:`count_free_ending_soon` —— 免费窗口 ≤ 6 小时内截止的种子数
- :func:`count_index_total` —— 索引总条数
- :func:`count_inflight_torrents` —— 下载器在途任务合计（带 30s 缓存）

**降级**：任一子查询失败 → 该字段返回 ``None``（前端显示 ``—``），
不影响其他字段，接口不返回 5xx。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from app.adapters.downloaders.base import TorrentState
from app.core.logging import get_logger
from app.core.simple_cache import TTLCache
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import CrossSeedSnapshot, DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType
from app.services.factory import get_factory

logger = get_logger(__name__)

__all__ = [
    "FREE_ENDING_SOON_HOURS",
    "count_free_ending_soon",
    "count_index_total",
    "count_inflight_torrents",
    "count_inflight_torrents_with_freshness",
    "site_and_downloader_counts",
]


#: 免费窗口"即将截止"的判定阈值（小时）
FREE_ENDING_SOON_HOURS = 6

#: 下载器在途任务数的缓存 TTL（秒）——前端 5s 轮询，30s TTL 足够新鲜
_INFLIGHT_TTL_SECONDS = 30.0

#: 进程内缓存（模块级单例；多进程部署时各进程各持一份，是刻意的）
_inflight_cache: TTLCache[int | None] = TTLCache(ttl_seconds=_INFLIGHT_TTL_SECONDS)


async def site_and_downloader_counts() -> tuple[int, int, int, int]:
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


async def count_free_ending_soon() -> int | None:
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


async def count_index_total() -> int | None:
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


async def count_inflight_torrents() -> int | None:
    """下载器在途任务合计（跨所有启用的下载器）。

    "在途"= 正在参与的任务：做种 / 下载 / 检查 / 停滞——**排除**已暂停与
    致命错误（error / missingFiles）的任务。

    刻意不排除"已完成"：PT 辅种场景下做种（seeding）正是主要活跃状态，
    排除它会让该指标恒为 0。

    **辅种下载器读快照，不实时拉**（2026-10-24）：辅种站的种子量大
    （实测 3000+ 个），实时拉一次要 20s+——仪表盘每 30 秒轮询一次，
    会把辅种站的连接长期占住，反而拖慢辅种页自己的手动刷新。故辅种
    部分取 ``cross_seed_snapshot.inflight_count``（定时作业写入的快照）。

    非辅种下载器维持实时拉——它们种子少，毫秒级完成。

    任一下载器不可达 → 整体返回 ``None``（前端显示 ``—``），避免用不完整
    的数据误导用户。**辅种下载器没有快照行时同样返回 ``None``**——
    不是 0（"还没抓过" ≠ "没有在途任务"）。
    """
    try:
        return await _inflight_cache.get_or_set(_load_inflight_torrents)
    except Exception as exc:
        logger.warning("仪表盘统计（在途任务）查询失败：%s", exc)
        return None


async def count_inflight_torrents_with_freshness() -> tuple[int | None, str | None]:
    """在途任务数 + 数据新鲜度（辅种快照的最旧抓取时刻）。

    新鲜度取**辅种快照里最旧**的 ``scanned_at``——最旧的那个才是
    "整体数据有多新"的上界。前端据此显示"数据来自 X 分钟前"。

    非辅种下载器是实时拉的，不影响新鲜度（取辅种快照的最旧值即可）。
    """
    total = await count_inflight_torrents()
    freshness = await snapshot_freshness()
    return total, freshness


async def snapshot_freshness() -> str | None:
    """辅种快照里最旧的抓取时刻（无快照 → None）.

    取**最旧**而非最新：辅种快照各自独立刷新，最旧的那个才是"整体
    数据有多新"的上界。
    """
    try:
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(func.min(CrossSeedSnapshot.scanned_at))
                )
            ).scalar_one()
        return str(row) if row else None
    except Exception as exc:
        logger.warning("仪表盘统计（快照新鲜度）查询失败：%s", exc)
        return None


async def _load_inflight_torrents() -> int | None:
    """实际统计各下载器在途任务数（缓存 loader）。

    辅种下载器走快照（DB 读），非辅种下载器走实时拉取。
    """
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
        snapshot_rows = (
            await session.execute(select(CrossSeedSnapshot))
        ).scalars().all()

    if not rows:
        return 0

    # 辅种下载器的在途数来自快照
    snapshot_inflight: dict[int, int] = {
        s.downloader_id: s.inflight_count for s in snapshot_rows
    }

    factory = get_factory()
    total = 0
    for row in rows:
        if row.is_cross_seed:
            # 辅种下载器：读快照。没有快照行 → 整体降级为 None
            # （"还没抓过" ≠ "0 个在途任务"）。
            inflight = snapshot_inflight.get(row.id)
            if inflight is None:
                logger.info(
                    "仪表盘统计：辅种下载器 %s 还没有快照，在途数不可用", row.name
                )
                return None
            total += inflight
            continue
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
    """是否为"在途"任务：非暂停、非致命错误。

    口径与 :attr:`TorrentState.is_inflight` 一致（单一来源，避免两处
    各写一份判定而漂移）。
    """
    state = info.state
    if not isinstance(state, TorrentState):
        return False
    return state.is_inflight
