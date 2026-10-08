"""队列重放（§6 可靠性/幂等性）。

进程崩溃后容器重启，``task_queue`` 里残留的 ``running`` 任务会被复位并重放。
幂等键保证重放**不产生重复下载器任务**（AC-09）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import AddTorrentPayload
from app.adapters.downloaders.locks import locks
from app.core.logging import audit, get_logger
from app.db.models import (
    AddHistory,
    DownloaderInstance,
    Source,
    Torrent,
    TorrentStatus,
    TriggerSource,
)
from app.db.session import session_scope
from app.services.factory import AdapterFactory
from app.services.task_queue import TaskKind, TaskQueueService

logger = get_logger(__name__)


async def replay_once(factory: AdapterFactory) -> int:
    """处理一批到期任务。返回处理条数。"""
    processed = 0
    async with session_scope() as session:
        queue = TaskQueueService(session)
        tasks = await queue.claim_due(limit=50)
        if not tasks:
            return 0

        for task in tasks:
            try:
                ok = await _execute(session, factory, task.kind, task.payload)
            except Exception as exc:
                await queue.mark_failed(task, str(exc))
                processed += 1
                continue

            if ok:
                await queue.mark_done(task)
            else:
                await queue.mark_failed(task, "执行失败")
            processed += 1

    if processed:
        logger.info("队列重放完成，处理 %d 个任务", processed)
    return processed


async def recover_on_start(factory: AdapterFactory) -> int:
    """启动时复位中断任务（崩溃恢复）。"""
    async with session_scope() as session:
        queue = TaskQueueService(session)
        count = await queue.requeue_running()
    if count:
        logger.warning("检测到 %d 个中断任务，已复位待重放", count)
    return count


async def _execute(
    session: AsyncSession,
    factory: AdapterFactory,
    kind: str,
    payload: dict[str, object],
) -> bool:
    """执行单个任务。目前支持 add_torrent。"""
    if kind != TaskKind.ADD_TORRENT:
        logger.info("任务类型 %s 尚未实现，跳过", kind)
        return True

    info_hash = str(payload.get("info_hash") or "")
    site_id = int(str(payload.get("site_id") or 0))
    downloader_id = int(str(payload.get("downloader_id") or 0))
    if not info_hash or not downloader_id:
        logger.warning("add_torrent 任务缺少必要字段：%s", payload)
        return False

    from sqlalchemy import select

    from app.db.models import Site

    # 幂等二次校验：DB 已有记录则直接判定完成（AC-09 三次重启不重复）
    exists = (
        await session.execute(
            select(Torrent.id).where(Torrent.info_hash == info_hash)
        )
    ).scalar_one_or_none()
    if exists is not None:
        logger.info("任务 %s 已存在，跳过重放（幂等）", info_hash)
        return True

    dl = (
        await session.execute(
            select(DownloaderInstance).where(DownloaderInstance.id == downloader_id)
        )
    ).scalar_one_or_none()
    site = (
        await session.execute(select(Site).where(Site.id == site_id))
    ).scalar_one_or_none()
    if dl is None or site is None:
        logger.warning("任务 %s 的下载器/站点已不存在，放弃", info_hash)
        return True

    adapter = factory.downloader(dl)
    url = str(payload.get("url") or "")
    if not url:
        return False

    with audit(
        "add_torrent",
        downloader=dl.name,
        source=TriggerSource.SCHEDULE,
        count=1,
        reason="queue_replay",
        logger=logger,
    ):
        async with locks.acquire(dl.name):
            await adapter.add_torrent(
                AddTorrentPayload(
                    name=str(payload.get("name") or info_hash),
                    urls=[url],
                    save_path=str(payload.get("save_path") or ""),
                    category=str(payload.get("category") or site.category),
                    tags=frozenset({"auto", f"site:{site.name}"}),
                )
            )

    from datetime import timedelta

    from app.core.timeutils import utcnow

    torrent = Torrent(
        info_hash=info_hash,
        name=str(payload.get("name") or info_hash)[:1024],
        site_id=site_id,
        downloader_id=downloader_id,
        category=str(payload.get("category") or site.category),
        save_path=str(payload.get("save_path") or ""),
        size=int(str(payload.get("size") or 0)),
        source=Source.AUTO_SEED,
        status=TorrentStatus.ACTIVE,
        protect_until=utcnow() + timedelta(hours=12),
    )
    session.add(torrent)
    await session.flush()
    session.add(
        AddHistory(
            site_id=site_id,
            info_hash=info_hash,
            torrent_id=torrent.id,
            name=str(payload.get("name") or info_hash)[:1024],
            source=Source.AUTO_SEED,
            outcome="added",
        )
    )
    return True