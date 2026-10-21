"""推送服务：手动推送与自动推送的**同一份实现**（FR-1 / 幂等性）。

背景：此前手动推送（``app/api/routes/torrent_index.py``）与自动推送
（``app/services/push_free.py`` → ``task_executor``）是两条**分叉**的实现：

- 手动推送：自己重写了一遍「下载 .torrent → 解析 hash → add → 标 pushed」
- 自动推送：走 ``task_queue`` → ``task_executor.execute_add_torrent``

两份逻辑已经漂移——手动推送有 TOCTOU 防御（``or_(IS NULL, != PUSHED)``
+ rowcount 检查），``task_executor`` 没有；手动推送也不走队列，
无崩溃恢复 / 幂等键 / 退避重试。

本模块把两者**合流**：都通过 ``task_queue`` 落库，都由
:func:`app.services.task_executor.execute_add_torrent` 执行。

手动推送选「入队 + 同步等待」而非「纯异步」：用户点了按钮就该拿到
结果。任务已落库，所以进程崩了 ``drain_task_queue`` 作业会接手重试
（不会丢）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import PushStatus
from app.services.downloader_query import get_default_downloader, get_downloader_by_id
from app.services.task_executor import PushItemError, execute_add_torrent
from app.services.task_queue import TaskKind, TaskQueueService, make_idem_key

if TYPE_CHECKING:
    from app.services.factory import AdapterFactory

logger = get_logger(__name__)


class PushError(RuntimeError):
    """推送失败（面向 API 层，直接转 HTTP 状态码）。

    :param status: 建议的 HTTP 状态码（404 / 400 / 502）
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """推送结果。"""

    ok: bool
    info_hash: str | None = None
    downloader_id: int | None = None
    detail: str = ""
    #: True = 幂等命中（已推过 / 下载器报 duplicate），未实际重复添加
    idempotent: bool = False


async def resolve_downloader(
    downloader_id: int | None,
) -> DownloaderInstance:
    """解析目标下载器。

    - 指定了 downloader_id → 用它（不存在 → 404，已禁用 → 400）
    - 未指定 → 用 ``is_default=True`` 且 enabled 的（没有 → 400）
    """
    if downloader_id is not None:
        dl = await get_downloader_by_id(downloader_id)
        if dl is None:
            raise PushError("下载器不存在", status=404)
        if not dl.enabled:
            raise PushError("下载器已禁用", status=400)
        return dl
    dl = await get_default_downloader()
    if dl is None:
        raise PushError("未配置默认下载器", status=400)
    return dl


async def push_index_entry(
    index_id: int,
    downloader_id: int | None,
    factory: AdapterFactory,
    *,
    category: str = "manual-push",
) -> PushOutcome:
    """把索引条目推送到下载器（手动推送入口）。

    流程：

    1. 读索引条目 + 站点 + 下载器（校验存在性与启用状态）
    2. 已推送过（``push_status == pushed``）→ 直接返回幂等成功
    3. **入队**（幂等键 = kind|site_id|item_id|dl=downloader_id）
    4. 认领并**同步执行**（``execute_add_torrent``）
    5. 成功 → mark_done；失败 → 按错误分类决定退避重试或永久失败

    步骤 3-4 与自动推送（``push_free``）走**完全相同**的队列与执行器，
    因此崩溃恢复 / 幂等 / 退避 / TOCTOU 防御全部一致。
    """
    # 1) 读索引 + 站点
    async with session_scope() as session:
        item = (
            await session.execute(
                select(TorrentIndex).where(TorrentIndex.id == index_id)
            )
        ).scalar_one_or_none()
        if item is None:
            raise PushError("索引条目不存在", status=404)
        if not item.item_id:
            raise PushError("该条目缺少 item_id，无法推送", status=400)
        site_id = item.site_id
        item_id = item.item_id
        push_status = item.push_status
        site_exists = (
            await session.execute(select(Site.id).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site_exists is None:
            raise PushError("站点不存在", status=404)

    dl = await resolve_downloader(downloader_id)
    dl_id = dl.id

    # 2) 已推送过 → 幂等成功（不重复推送）
    if push_status == PushStatus.PUSHED:
        return PushOutcome(
            ok=True,
            downloader_id=dl_id,
            detail="该种已推送过",
            idempotent=True,
        )

    # 3) 入队（幂等）
    idem_key = make_idem_key(
        TaskKind.ADD_TORRENT,
        site_id=site_id,
        info_hash=item_id,
        extra=f"dl={dl_id}",
    )
    payload: dict[str, object] = {
        "index_id": index_id,
        "downloader_id": dl_id,
    }
    async with session_scope() as session:
        tq = TaskQueueService(session)
        enq = await tq.enqueue(TaskKind.ADD_TORRENT, payload, idem_key=idem_key)
        if not enq.created:
            # 幂等命中：已有等价任务（已推过 / 在推 / 待重试）
            return PushOutcome(
                ok=True,
                downloader_id=dl_id,
                detail=f"该种已在推送队列中（状态：{enq.existing_status}）",
                idempotent=True,
            )
        task = await tq.claim_one(enq.task_id)
    if task is None:
        # 极小概率：入队成功但认领不到（并发抢走）——视为幂等成功
        return PushOutcome(
            ok=True,
            downloader_id=dl_id,
            detail="该种正在被其他请求推送",
            idempotent=True,
        )

    # 4) 同步执行
    try:
        result = await execute_add_torrent(payload, factory, category=category)
    except PushItemError as exc:
        async with session_scope() as session:
            tq = TaskQueueService(session)
            if exc.category == "fatal":
                await tq.mark_failed_forever(task.id, str(exc))
            else:
                await tq.mark_failed(task.id, str(exc))
        logger.warning("手动推送 index_id=%d 失败：%s", index_id, exc)
        raise PushError(str(exc), status=502) from exc

    # 5) 成功 → mark_done
    async with session_scope() as session:
        tq = TaskQueueService(session)
        await tq.mark_done(task.id)

    info_hash = str(result.get("info_hash") or "")
    idempotent = bool(result.get("idempotent"))
    detail = "推送成功" if not idempotent else "该种已存在于下载器（幂等成功）"
    return PushOutcome(
        ok=True,
        info_hash=info_hash or None,
        downloader_id=dl_id,
        detail=detail,
        idempotent=idempotent,
    )
