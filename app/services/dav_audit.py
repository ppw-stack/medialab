"""WebDAV 请求审计。

**异步写**：审计不能拖慢请求 —— 写库是 IO 操作，PROPFIND 一次扫几百个
目录，逐条同步写会把延迟放大到不可接受。故用队列 + 批量 flush。

**粒度**（避免表爆炸）：

- ``GET`` / ``HEAD`` —— **逐条明细**（谁拿了哪个文件；这是审计的价值）
- ``PROPFIND`` —— **汇总**一条，带 ``item_count``
- 认证失败 —— 逐条（IP + 用户名 + UA）

**留存**：默认 90 天（``dav_audit_retention_days``），超时自动清理。

**队列与 event loop**：``asyncio.Queue`` 在 Python 3.10+ 会**绑定创建它的
loop**。进程级单例跨 loop 使用（如测试里每个用例一个新 loop）会抛
``Queue is bound to a different event loop``。故检测到 loop 变化就重建
队列 —— 旧队列里的条目丢弃（它们属于上一个 loop，flush 不了也没意义）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete

from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import DavAuditLog
from app.db.session import session_scope

logger = get_logger(__name__)

#: 队列里攒多少条就 flush 一次
FLUSH_BATCH = 50

#: 最多攒多久就 flush（秒）——低流量时也要保证审计及时落库
FLUSH_INTERVAL_SEC = 5.0


@dataclass(frozen=True)
class AuditEntry:
    """一条审计记录。"""

    user_id: int
    username: str
    method: str
    path: str
    status: int
    client: str = ""
    remote_addr: str = ""
    duration_ms: int = 0
    item_count: int = 0
    created_at: str = ""


class DavAuditor:
    """审计写入器（进程内单例）。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AuditEntry] = asyncio.Queue(maxsize=1000)
        self._queue_loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        #: 因队列满而丢弃的条数（监控用）
        self.dropped = 0

    # -- 生命周期 --

    def _ensure_queue(self) -> asyncio.Queue[AuditEntry]:
        """确保队列属于**当前** event loop。

        见模块文档：``asyncio.Queue`` 绑定创建它的 loop，跨 loop 使用会崩。
        """
        loop = asyncio.get_running_loop()
        if self._queue_loop is not loop:
            self._queue = asyncio.Queue(maxsize=1000)
            self._queue_loop = loop
            self._task = None
        return self._queue

    def start(self) -> None:
        """启动后台 flush 协程（幂等）。"""
        if self._stopped:
            return
        self._ensure_queue()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        """停机：把队列里的剩余条目落库。"""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._flush()

    # -- 写入 --

    def record(
        self,
        *,
        user_id: int,
        username: str,
        method: str,
        path: str,
        status: int,
        client: str = "",
        remote_addr: str = "",
        duration_ms: int = 0,
        item_count: int = 0,
    ) -> None:
        """记一条审计（**非阻塞**）。"""
        queue = self._ensure_queue()
        max_len = get_settings().dav_audit_client_max_len
        entry = AuditEntry(
            user_id=user_id,
            username=username,
            method=method,
            path=path[:1024],
            status=status,
            client=client[:max_len],
            remote_addr=remote_addr[:64],
            duration_ms=duration_ms,
            item_count=item_count,
            created_at=bj_now(),
        )
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            # 队列满 —— 丢弃并计数。**不能阻塞请求**（审计不该拖垮服务）
            self.dropped += 1
            logger.warning("审计队列已满，丢弃一条记录（累计 %d）", self.dropped)

    # -- 后台 flush --

    async def _loop(self) -> None:
        """周期性批量落库。"""
        queue = self._ensure_queue()
        while not self._stopped:
            try:
                # **先取一条**（这一步已经消费了队列里的条目）
                first = await asyncio.wait_for(queue.get(), timeout=FLUSH_INTERVAL_SEC)
            except TimeoutError:
                await self._flush()
                continue
            except asyncio.CancelledError:
                await self._flush()
                raise

            # 第一条已经拿到了，再把队列里剩下的取完凑一批。
            #
            # **注意**：这里不能再用 `if not queue.empty()` 判断后再取 ——
            # 那正是旧实现丢条目的 bug：判断与取之间有竞态，且 `first`
            # 已经被消费却没被计入 `entries`。
            entries = [first]
            while len(entries) < FLUSH_BATCH:
                try:
                    entries.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._write(entries)

    async def _flush(self) -> None:
        """把队列里剩下的全部落库。"""
        queue = self._ensure_queue()
        entries: list[AuditEntry] = []
        while True:
            try:
                entries.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if entries:
            await self._write(entries)

    async def _write(self, entries: list[AuditEntry]) -> None:
        """批量写库（失败只记日志，不影响请求）。"""
        if not entries:
            return
        try:
            async with session_scope() as db:
                for e in entries:
                    db.add(
                        DavAuditLog(
                            user_id=e.user_id,
                            username=e.username,
                            method=e.method,
                            path=e.path,
                            status=e.status,
                            client=e.client,
                            remote_addr=e.remote_addr,
                            duration_ms=e.duration_ms,
                            item_count=e.item_count,
                            created_at=e.created_at,
                        )
                    )
            logger.debug("已写入 %d 条 WebDAV 审计", len(entries))
        except Exception:
            logger.exception("写 WebDAV 审计失败（%d 条）", len(entries))

    def stats(self) -> dict[str, int]:
        """审计器状态（监控用）。"""
        return {
            "queued": self._queue.qsize(),
            "dropped": self.dropped,
        }


#: 进程级审计器
dav_auditor = DavAuditor()


async def cleanup_audit(settings: AppSettings | None = None) -> int:
    """删除超出留存期的审计记录，返回删除条数。

    留存期由 ``dav_audit_retention_days`` 控制；0 = 永久保留（不删）。
    """
    settings = settings or get_settings()
    days = settings.dav_audit_retention_days
    if days <= 0:
        return 0

    cutoff = format_bj(bj_now_dt() - timedelta(days=days))
    async with session_scope() as db:
        result = await db.execute(delete(DavAuditLog).where(DavAuditLog.created_at < cutoff))
        deleted = int(getattr(result, "rowcount", 0) or 0)
    if deleted:
        logger.info("清理过期 WebDAV 审计 %d 条（留存 %d 天）", deleted, days)
    return deleted
