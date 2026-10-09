"""M4 中止产物 → M5 Cleaner 的桥梁（FR-3.5 + FR-5.2 共用管线）。

设计原则：

- **M4 只写意图**：WindowWatcher 发现任务到红线时只标记 ``aborted`` 并
  入队 ``abort_window`` 任务，**不**直接碰下载器或文件。
- **M5 Cleaner 统一执行**：`execute_pending_aborts()` 取出所有 pending 的
  ``abort_window`` 任务，走与 F5 清理完全相同的六重保护管线。

这样做的好处：
1. 单一执行路径，六重保护不会被绕过
2. 中止与清理共用幂等键（``task_queue``），重放安全
3. 中止可被二次确认门阻拦（FR-5.2.4）—— 异常中止风暴时人工介入
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Torrent, TorrentStatus
from app.db.session import session_scope
from app.services.cleanup import Cleaner, CleanupOutcome
from app.services.factory import AdapterFactory
from app.services.task_queue import TaskKind, TaskQueueService, make_idem_key

logger = get_logger(__name__)


async def execute_pending_aborts(factory: AdapterFactory) -> CleanupOutcome:
    """处理 task_queue 中所有 pending 的 abort_window 任务。

    与 ``replay_once`` 平级：拉一批到期任务，逐条走 Cleaner 管线。
    中止任务与清理任务共用 Cleaner——这意味着中止走完整六重保护，
    不会被绕过。
    """
    out = CleanupOutcome()

    async with session_scope() as session:
        queue = TaskQueueService(session)
        tasks = await queue.claim_due(limit=50, kind=TaskKind.ABORT_WINDOW)
        aborts = tasks
        if not aborts:
            return out

        cleaner = Cleaner(session, factory)
        for task in aborts:
            payload = task.payload or {}
            site_id = int(str(payload.get("site_id") or 0))
            info_hash = str(payload.get("info_hash") or "")
            reason = str(payload.get("reason") or "window_expired")
            ok = await _execute_one_abort(cleaner, session, info_hash, site_id, reason)
            if ok:
                await queue.mark_done(task)
                out.candidates_executed += 1
            else:
                await queue.mark_failed(task, "abort_pipeline_failed")
                out.blocked_reasons["abort_failed"] = (
                    out.blocked_reasons.get("abort_failed", 0) + 1
                )
        out.candidates_total = len(aborts)

    if out.candidates_executed:
        logger.info(
            "M5 Cleaner 处理中止：执行 %d / 失败 %d",
            out.candidates_executed,
            out.blocked_reasons.get("abort_failed", 0),
        )
    return out


async def _execute_one_abort(
    cleaner: Cleaner,
    session: AsyncSession,
    info_hash: str,
    site_id: int,
    reason: str,
) -> bool:
    """单条中止任务走 Cleaner 管线。

    中止在 Cleaner 中复用清理规则：reason 与规则字段对齐（window_expired
    → 命中 ``status=aborted`` 类规则）。如果用户没配这种规则，中止也会
    走白名单 / 保护期 / 路径三重校验后才移入回收站。
    """
    t = (
        await session.execute(
            select(Torrent).where(Torrent.info_hash == info_hash)
        )
    ).scalar_one_or_none()
    if t is None:
        logger.warning("中止任务找不到 torrent：%s", info_hash)
        return True  # 已删除视为成功
    if t.status == TorrentStatus.DELETED:
        return True  # 已在回收站

    # 不构造 CleanupRule——直接复用 Cleaner 的内部清理路径。
    download_root = await cleaner._resolve_download_root([t])
    if download_root is None:
        logger.warning("中止产物 %s 无法定位下载根目录", info_hash)
        return False
    # 这里走 _quarantine_one 的逻辑：先 add_history 标记 → remove(d=false)
    # → 移入回收站。
    return await cleaner._quarantine_one(
        __import__("app.services.cleanup", fromlist=["CleanupCandidate"]).CleanupCandidate(
            torrent=t,
            rule=None,
            reason=f"abort:{reason}",
            size=t.size,
        ),
        download_root=download_root,
        policy=await cleaner._load_policy(),
        trigger_source="window_abort",
    )


def make_abort_idem_key(site_id: int, info_hash: str, reason: str) -> str:
    """M4 中止指令的幂等键（与 M5 Cleaner 共享）。"""
    return make_idem_key(
        TaskKind.ABORT_WINDOW,
        site_id=site_id,
        info_hash=info_hash,
        extra=reason,
    )
