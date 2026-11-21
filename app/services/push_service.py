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

from sqlalchemy import select, update

from app.core.logging import get_logger
from app.db.models import DownloaderInstance, Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import PushStatus
from app.services.downloader_query import get_default_downloader, get_downloader_by_id
from app.services.task_executor import PushItemError, execute_add_torrent
from app.services.task_queue import (
    MAX_ATTEMPTS,
    TaskKind,
    TaskQueueBusyError,
    TaskQueueService,
    TaskStatus,
    claim_with_retry,
    enqueue_with_retry,
    make_idem_key,
)

if TYPE_CHECKING:
    from app.services.factory import AdapterFactory

logger = get_logger(__name__)


class PushError(RuntimeError):
    """推送失败（面向 API 层，直接转 HTTP 状态码）。

    :param status: 建议的 HTTP 状态码（404 / 400 / 502）
    :param code: 机器可读错误码——**批量推送按它分类**（跳过 vs 失败），
        不靠 message 文案匹配（文案会改，码不会）。
    """

    def __init__(self, message: str, *, status: int = 400, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """推送结果。"""

    ok: bool
    info_hash: str | None = None
    downloader_id: int | None = None
    detail: str = ""
    #: True = 幂等命中（已推过 / 下载器报 duplicate），未实际重复添加
    idempotent: bool = False
    #: True = 请求已受理但**尚未完成**：任务留在队列里按退避自动重试
    #: （1/2/4/8 分钟，最多 ``MAX_ATTEMPTS`` 次）。
    #:
    #: 典型场景 = 站点瞬时返回非 .torrent 内容（HTML 提示页 / 会话检查页）。
    #: API 层据此返回 **202**，前端提示「已入队重试」而不是「推送失败」。
    #: 2026-11-21 prod：手动推送 12:05 报 502，队列 12:10 补推成功——
    #: 用户看到的「失败」与最终结果不一致，故引入本字段。
    #:
    #: ⚠️ **只用于「重试有意义」的失败**：站点的永久失败（凭据失效 / 详情页
    #: 里没有下载地址）与「重试预算已用尽」都走 **502**——那种情况下报
    #: 202「已入队自动重试」是**说谎**（队列里已经是死信，不会再自动跑）。
    deferred: bool = False


async def resolve_downloader(
    downloader_id: int | None,
) -> DownloaderInstance:
    """解析目标下载器。

    - 指定了 downloader_id → 用它（不存在 → 404，已禁用 → 400）
    - 未指定 → 用 ``is_default=True`` 且 enabled 的（没有 → 400）

    :raises PushError: 下载器不存在（``status=404`` /
        ``code=downloader_not_found``）/ 下载器已禁用（``status=400`` /
        ``code=downloader_disabled``）/ 未配置默认下载器（``status=400`` /
        ``code=no_default_downloader``）
    """
    if downloader_id is not None:
        dl = await get_downloader_by_id(downloader_id)
        if dl is None:
            raise PushError(
                "下载器不存在", status=404, code="downloader_not_found"
            )
        if not dl.enabled:
            raise PushError(
                "下载器已禁用", status=400, code="downloader_disabled"
            )
        return dl
    dl = await get_default_downloader()
    if dl is None:
        raise PushError(
            "未配置默认下载器", status=400, code="no_default_downloader"
        )
    return dl


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    """入队结果（**不含执行**）。

    :param state: ``enqueued``（新入队）/ ``already_pushed``（已推过）/
        ``already_queued``（队列里已有等价任务）
    """

    state: str
    dl_id: int
    detail: str
    task_id: int | None = None
    payload: dict[str, object] | None = None


async def enqueue_index_entry(index_id: int, downloader_id: int | None) -> EnqueueOutcome:
    """把索引条目**入队**（校验 + 幂等判定 + 入队，**不执行**）。

    与 :func:`push_index_entry` 共享前 3 步——单条与批量走**同一份**校验 /
    幂等 / 入队逻辑，不会漂移。差别只在于是否同步执行：

    - 单条手动推送：入队后立刻认领并执行 → 用户当场拿到结果
    - 批量推送：只入队，由 ``drain_task_queue`` 按队列节流消化（一次几百条
      不能挂住 HTTP 请求）

    :raises PushError: 索引条目不存在（``status=404`` /
        ``code=not_found``）/ 缺 item_id（``status=400`` /
        ``code=no_link``）/ 站点不存在（``status=404`` /
        ``code=site_missing``）/ 站点已停用（``status=400`` /
        ``code=site_disabled``）/ 队列繁忙（``status=503`` /
        ``code=db_busy``）/ 下载器不可用（见 :func:`resolve_downloader`）
    """
    # 1) 读索引 + 站点
    async with session_scope() as session:
        item = (
            await session.execute(
                select(TorrentIndex).where(TorrentIndex.id == index_id)
            )
        ).scalar_one_or_none()
        if item is None:
            raise PushError("索引条目不存在", status=404, code="not_found")
        if not item.item_id:
            raise PushError(
                "该条目缺少 item_id，无法推送", status=400, code="no_link"
            )
        site_id = item.site_id
        item_id = item.item_id
        push_status = item.push_status
        site_row = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site_row is None:
            raise PushError("站点不存在", status=404, code="site_missing")
        if not site_row.enabled:
            # 站点停用 → 不推（与自动推送的候选过滤、执行层兜底同语义）。
            #
            # 为什么在**入队前**拦：批量推送只入队不执行，停了站点还入队
            # 的话，任务会被 drain 执行成 fatal 死信——既没推成，又占住
            # 幂等键（重新启用站点后该种子要等队列清理才能重推）。
            raise PushError(
                f"站点「{site_row.name}」已停用，无法推送",
                status=400,
                code="site_disabled",
            )

    dl = await resolve_downloader(downloader_id)
    dl_id = dl.id

    # 2) 已推送过 → 幂等（不再入队）
    if push_status == PushStatus.PUSHED:
        return EnqueueOutcome(
            state="already_pushed", dl_id=dl_id, detail="该种已推送过"
        )

    # 3) 入队（幂等键 = kind|site_id|item_id|dl=downloader_id）
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
    # 入队撞 SQLite 写锁时退避重试（2026-11-12 prod 事故：扫描批量写库
    # 期间手动推送的 INSERT 等满 10s 仍失败 → 用户点按钮直接报错）。
    try:
        enq = await enqueue_with_retry(
            TaskKind.ADD_TORRENT, payload, idem_key=idem_key
        )
    except TaskQueueBusyError as exc:
        raise PushError(
            f"推送队列繁忙（数据库忙），请稍后重试：{exc}",
            status=503,
            code="db_busy",
        ) from exc
    if not enq.created:
        # 幂等命中：已有等价任务（已推过 / 在推 / 待重试）
        #
        # **死信（failed）例外**：手动重推必须能重新执行——``enqueue`` 对
        # 已存在的幂等键一律返回 created=False，死信会让用户「再点一次也
        # 推不上去」（2026-11-21 Kody 审查发现：重试耗尽后任务已是死信，
        # 再点只拿到 200「已在推送队列中（状态：failed）」）。
        # 故显式重置死信任务（attempts 归零 + 回到 pending）再执行。
        if enq.existing_status == TaskStatus.FAILED:
            async with session_scope() as session:
                requeued = await TaskQueueService(session).requeue_failed(
                    enq.task_id
                )
            if requeued:
                # 重置死信时**同步清掉索引行上的旧推送状态**。
                #
                # ① 防止「假成功」：索引行若留着 ``push_status='pushed'``，
                #    重推会在 execute_add_torrent 的幂等短路处直接返回
                #    成功——种子压根没进下载器，死信却被「成功」掩埋。
                # ② 状态一致：重试在途时索引行不该还显示「失败」。
                #
                # 只清「失败 / 未推过」的行——``pushed`` 的行在第 2 步
                # （已推过 → 幂等成功）就返回了，根本走不到这里。
                await _clear_push_failure(index_id)
                return EnqueueOutcome(
                    state="enqueued",
                    dl_id=dl_id,
                    detail="上一轮推送失败（重试已用尽），已重置并重新入队",
                    task_id=enq.task_id,
                    payload=payload,
                )
        return EnqueueOutcome(
            state="already_queued",
            dl_id=dl_id,
            detail=f"该种已在推送队列中（状态：{enq.existing_status}）",
        )
    task_id = enq.task_id
    return EnqueueOutcome(
        state="enqueued",
        dl_id=dl_id,
        detail="已入队",
        task_id=task_id,
        payload=payload,
    )

async def _clear_push_failure(index_id: int) -> None:
    """清掉索引行的「推送失败」标记（手动重推死信时调用）。

    只清 ``push_status`` / ``pushed_at``：``info_hash`` 留着（它是种子自身
    的属性，与推送成败无关）。
    """
    async with session_scope() as session:
        await session.execute(
            update(TorrentIndex)
            .where(TorrentIndex.id == index_id)
            .values(push_status=None, pushed_at=None)
        )


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

    前 3 步抽到 :func:`enqueue_index_entry`——批量推送复用同一份逻辑。

    :returns: :class:`PushOutcome`。**失败也可能是返回值而不是异常**：
        可重试的下载失败返回 ``ok=False, deferred=True``（API 据此回
        202，任务留在队列里继续重试）。
    :raises PushError: 索引条目不存在（``status=404`` /
        ``code=not_found``）/ 缺 item_id（``status=400`` /
        ``code=no_link``）/ 站点不存在（``status=404`` /
        ``code=site_missing``）/ 站点已停用（``status=400`` /
        ``code=site_disabled``）/ 队列繁忙（``status=503`` /
        ``code=db_busy``）/ 下载器不可用（见 :func:`resolve_downloader`）/
        推送失败（``status=502`` / ``code=push_failed``——含解析失败、
        站点永久失败、重试预算耗尽；这几种重试也不会成功）
    """
    enq = await enqueue_index_entry(index_id, downloader_id)
    if enq.state in ("already_pushed", "already_queued"):
        return PushOutcome(
            ok=True,
            downloader_id=enq.dl_id,
            detail=enq.detail,
            idempotent=True,
        )
    payload = enq.payload
    task_id = enq.task_id
    assert payload is not None and task_id is not None  # enqueued 分支必有

    # 认领并同步执行（认领同样撞锁时退避重试）
    try:
        claimed = await claim_with_retry(task_id)
    except TaskQueueBusyError as exc:
        raise PushError(
            f"推送队列繁忙（数据库忙），请稍后重试：{exc}",
            status=503,
            code="db_busy",
        ) from exc
    if not claimed:
        # 极小概率：入队成功但认领不到（并发抢走）——视为幂等成功
        return PushOutcome(
            ok=True,
            downloader_id=enq.dl_id,
            detail="该种正在被其他请求推送",
            idempotent=True,
        )

    # 4) 同步执行
    try:
        result = await execute_add_torrent(payload, factory, category=category)
    except PushItemError as exc:
        final_state: str
        async with session_scope() as session:
            tq = TaskQueueService(session)
            if exc.category == "fatal":
                await tq.mark_failed_forever(task_id, str(exc))
                final_state = TaskStatus.FAILED
            else:
                final_state = await tq.mark_failed(task_id, str(exc))
        if exc.category == "download":
            # **下载失败 = 瞬时故障**（网络抖动 / 站点临时错误页）：任务已
            # 在队列里，``drain_task_queue`` 会按指数退避继续重试。实测这条
            # 链路会「5 分钟后补推成功」（2026-11-21 prod：12:05 首轮失败 →
            # 12:10 成功）。若此刻向用户报「推送失败」+502，用户会以为种子
            # 丢了并反复点击——因此改为 deferred（API 202 +「已入队重试」）。
            #
            # ⚠️ 站点的**永久失败**（凭据失效 / 详情页里没有下载地址）已被
            # 执行层判为 ``fatal``，不进这里——那种情况重试 5 次也不会成功，
            # 报「已入队自动重试」是骗人。
            if final_state == TaskStatus.FAILED:
                # 重试预算用尽 → 任务已成**死信**，不会再自动重试。此时报
                # 202「已入队自动重试」是**说谎**（队列里已经是 failed）→
                # 走 502，并提示重新点击推送（入队时会重置死信任务，
                # 见 :func:`enqueue_index_entry`）。
                detail = (
                    f"站点下载失败且重试已用尽（{MAX_ATTEMPTS} 次），"
                    "再次点击「推送」可重试"
                )
                logger.warning(
                    "手动推送 index_id=%d 下载失败且重试用尽，任务 %d 状态=%s：%s",
                    index_id, task_id, final_state, exc,
                )
                raise PushError(
                    f"{detail}：{str(exc)[:180]}", status=502, code="push_failed"
                ) from exc
            detail = "站点暂时异常，已入队自动重试（最多 5 次），无需重复点击"
            logger.warning(
                "手动推送 index_id=%d 下载失败，任务 %d 状态=%s：%s",
                index_id, task_id, final_state, exc,
            )
            return PushOutcome(
                ok=False,
                downloader_id=enq.dl_id,
                detail=f"{detail}：{str(exc)[:180]}",
                deferred=True,
            )
        logger.warning("手动推送 index_id=%d 失败：%s", index_id, exc)
        raise PushError(str(exc), status=502, code="push_failed") from exc

    # 5) 成功 → mark_done
    async with session_scope() as session:
        tq = TaskQueueService(session)
        await tq.mark_done(task_id)

    info_hash = str(result.get("info_hash") or "")
    idempotent = bool(result.get("idempotent"))
    detail = "推送成功" if not idempotent else "该种已存在于下载器（幂等成功）"
    return PushOutcome(
        ok=True,
        info_hash=info_hash or None,
        downloader_id=enq.dl_id,
        detail=detail,
        idempotent=idempotent,
    )
