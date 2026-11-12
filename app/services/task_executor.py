"""任务执行器：把 task_queue 中的任务载荷翻译成实际动作。

目前实现 ``ADD_TORRENT``（免费种推送）。幂等设计（崩溃恢复安全重试）：

1. ``torrent_index.push_status == pushed`` → 直接成功。覆盖「add_torrent 成功
   但 push_status 更新前进程崩溃」的窗口。
2. 下载器报 duplicate / already → 视为成功（种子已在下载器里）。TR 原生
   返回 ``torrent-duplicate`` 不抛错；qB 重复种子在 v5 以 failure_count>0
   报错，文案含 duplicate / already。

错误分类（``PushItemError.category``）：
- ``download`` / ``parse`` / ``add`` —— 瞬态失败，mark_failed 退避重试
- ``fatal`` —— 站点**已停用**/不存在、下载器已禁用/不存在、索引行已不存在
  等契约错误，mark_failed_forever 不重试（重试只会反复失败）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, or_, select, update

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.adapters.sites._bencode import infohash_from_torrent
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import PushStatus
from app.services.task_queue import TaskKind

if TYPE_CHECKING:
    from app.services.factory import AdapterFactory

logger = get_logger(__name__)


class PushItemError(RuntimeError):
    """推送单项失败。``category`` 供调用方分类计数。"""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


#: duplicate 类错误的文案特征（qB 重复种子报错用）
_DUPLICATE_MARKERS = ("duplicate", "already", "已存在", "already exists")


def _is_duplicate_error(exc: DownloaderError) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _DUPLICATE_MARKERS)


async def execute_add_torrent(
    payload: dict[str, Any],
    factory: AdapterFactory,
    *,
    category: str = "auto-push",
) -> dict[str, Any]:
    """执行 ADD_TORRENT 任务：下载 .torrent → 解析 hash → 推送 → 更新索引。

    :param category: 推送到下载器时用的分类标签。自动推送是 ``auto-push``，
        手动推送是 ``manual-push``（供用户在下载器里区分来源）。
    :return: ``{"info_hash": str, "name": str, "idempotent": bool}``；
        ``idempotent=True`` 表示幂等命中（已推过 / duplicate），未实际重复添加。
    """
    index_id = int(payload["index_id"])
    downloader_id = int(payload["downloader_id"])

    # 1) 读索引行 + 幂等检查 + 站点行 + 下载器行
    async with session_scope() as session:
        item = (
            await session.execute(
                select(TorrentIndex).where(TorrentIndex.id == index_id)
            )
        ).scalar_one_or_none()
        if item is None:
            raise PushItemError("fatal", f"索引条目 id={index_id} 已不存在")
        if item.push_status == PushStatus.PUSHED:
            return {
                "info_hash": item.info_hash or "",
                "name": item.name,
                "idempotent": True,
            }
        if not item.item_id:
            raise PushItemError("fatal", f"索引条目 id={index_id} 缺少 item_id")
        # 提取标量到本地变量，session 关闭后不依赖 detached ORM 实例
        item_id = item.item_id
        name = item.name
        site_id = item.site_id
        site = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        dl = (
            await session.execute(
                select(DownloaderInstance).where(
                    DownloaderInstance.id == downloader_id
                )
            )
        ).scalar_one_or_none()
        # 同样提取 site/dl 的所需字段（session 关闭后访问标量虽安全——
        # expire_on_commit=False，但显式提取更清晰且防未来变更）
        if site is not None:
            site_row = site  # 保留引用给 factory.site()
            site_enabled = site.enabled
        if dl is not None:
            dl_enabled = dl.enabled
            dl_save_path = dl.save_path
            dl_row = dl  # 保留引用给 factory.downloader()
    if site is None:
        raise PushItemError("fatal", f"站点 id={site_id} 已不存在")
    if not site_enabled:
        # 站点被停用 → 不推（fatal：重试也不可能成功，直接死信）。
        #
        # 为什么执行层也要拦：候选查询（push_free）已经过滤 enabled 站点，
        # 但**停用前已入队**的 pending/failed 任务会被 drain_task_queue 重试
        # ——那正是「站点已停用却还在推」的第二条路径。
        raise PushItemError("fatal", f"站点 id={site_id} 已停用")
    if dl is None:
        raise PushItemError("fatal", f"下载器 id={downloader_id} 已不存在")
    if not dl_enabled:
        raise PushItemError("fatal", f"下载器 id={downloader_id} 已禁用")

    # 2) 下载 .torrent 文件
    site_adapter = factory.site(site_row)
    try:
        torrent_bytes = await site_adapter.download_torrent(item_id)
    except Exception as exc:
        raise PushItemError("download", f"download_torrent: {exc}") from exc
    if not torrent_bytes or not torrent_bytes.startswith(b"d"):
        raise PushItemError("download", "响应不是 bencoded torrent")

    # 3) 解析 info_hash
    info_hash = infohash_from_torrent(torrent_bytes)
    if info_hash is None:
        raise PushItemError("parse", "infohash 解析失败")

    # 4) 推送到下载器（duplicate → 幂等成功）
    dl_adapter = factory.downloader(dl_row)
    idempotent = False
    try:
        await dl_adapter.add_torrent(
            AddTorrentPayload(
                name=name,
                torrent_bytes=torrent_bytes,
                category=category,
                save_path=dl_save_path or None,
            )
        )
    except DownloaderError as exc:
        if _is_duplicate_error(exc):
            idempotent = True
        else:
            raise PushItemError("add", f"add_torrent: {exc}") from exc

    # 5) 更新 torrent_index.push_status = pushed
    #
    # TOCTOU 防御：加 `push_status != 'pushed'` 条件并检查 affected_rows——
    # 两个并发执行者都通过了第 1 步的幂等检查时，只有第一个 update 会命中行
    # （push_status 仍非 pushed），第二个的 rowcount=0 表示已被并发推送，
    # 本次视为幂等成功。
    #
    # 注意 SQL 三值逻辑：`push_status != 'pushed'` 对 NULL 求值为 NULL
    # （不匹配）。而 push_status 列 nullable，NULL = "从未推过"——正是本
    # 方法要更新的行！故必须用 or_(IS NULL, != PUSHED) 显式覆盖。
    async with session_scope() as session:
        result = cast(
            CursorResult[object],
            await session.execute(
                update(TorrentIndex)
                .where(
                    TorrentIndex.id == index_id,
                    or_(
                        TorrentIndex.push_status.is_(None),
                        TorrentIndex.push_status != PushStatus.PUSHED,
                    ),
                )
                .values(
                    push_status=PushStatus.PUSHED,
                    pushed_at=bj_now(),
                    info_hash=info_hash,
                )
            ),
        )
    if result.rowcount == 0:
        logger.info(
            "推送 index_id=%d：并发执行者已先推送，本次视为幂等成功", index_id
        )
    return {"info_hash": info_hash, "name": name, "idempotent": idempotent}


async def execute_task(
    kind: str, payload: dict[str, Any], factory: AdapterFactory
) -> dict[str, Any]:
    """按任务类型分发执行。"""
    if kind == TaskKind.ADD_TORRENT:
        return await execute_add_torrent(payload, factory)
    raise PushItemError("fatal", f"未知任务类型：{kind}")


__all__ = ["PushItemError", "execute_add_torrent", "execute_task"]
