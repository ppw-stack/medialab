"""任务执行器：把 task_queue 中的任务载荷翻译成实际动作。

目前实现 ``ADD_TORRENT``（免费种推送）。幂等设计（崩溃恢复安全重试）：

1. ``torrent_index.push_status == pushed`` → 直接成功。覆盖「add_torrent 成功
   但 push_status 更新前进程崩溃」的窗口。
2. 下载器报 duplicate / already → 视为成功（种子已在下载器里）。TR 原生
   返回 ``torrent-duplicate`` 不抛错；qB 重复种子在 v5 以 failure_count>0
   报错，文案含 duplicate / already。

错误分类（``PushItemError.category``）：
- ``download`` / ``parse`` / ``add`` —— 瞬态失败，mark_failed 退避重试
- ``fatal`` —— 站点/下载器/索引行已不存在等契约错误，mark_failed_forever
  不重试（重试只会反复失败）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from app.adapters.downloaders.base import AddTorrentPayload, DownloaderError
from app.adapters.sites._bencode import infohash_from_torrent
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import (
    DownloaderInstance,
    PushStatus,
    Site,
    TorrentIndex,
)
from app.db.session import session_scope
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
    payload: dict[str, Any], factory: AdapterFactory
) -> dict[str, Any]:
    """执行 ADD_TORRENT 任务：下载 .torrent → 解析 hash → 推送 → 更新索引。

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
        item_id = item.item_id
        name = item.name
        site = (
            await session.execute(select(Site).where(Site.id == item.site_id))
        ).scalar_one_or_none()
        dl = (
            await session.execute(
                select(DownloaderInstance).where(
                    DownloaderInstance.id == downloader_id
                )
            )
        ).scalar_one_or_none()
    if site is None:
        raise PushItemError("fatal", f"站点 id={item.site_id} 已不存在")
    if dl is None:
        raise PushItemError("fatal", f"下载器 id={downloader_id} 已不存在")
    if not dl.enabled:
        raise PushItemError("fatal", f"下载器 id={downloader_id} 已禁用")

    # 2) 下载 .torrent 文件
    site_adapter = factory.site(site)
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
    dl_adapter = factory.downloader(dl)
    idempotent = False
    try:
        await dl_adapter.add_torrent(
            AddTorrentPayload(
                name=name,
                torrent_bytes=torrent_bytes,
                category="auto-push",
                save_path=dl.save_path or None,
            )
        )
    except DownloaderError as exc:
        if _is_duplicate_error(exc):
            idempotent = True
        else:
            raise PushItemError("add", f"add_torrent: {exc}") from exc

    # 5) 更新 torrent_index.push_status = pushed
    async with session_scope() as session:
        await session.execute(
            update(TorrentIndex)
            .where(TorrentIndex.id == index_id)
            .values(
                push_status=PushStatus.PUSHED,
                pushed_at=bj_now(),
                info_hash=info_hash,
            )
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
