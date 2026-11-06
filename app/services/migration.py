"""115 → 光鸭 迁移引擎（一次性手动同步）。

**原理**：115 直链的 ``f=1`` 绑定「换链时用的 UA」。光鸭下载器有固定 UA
（:data:`app.adapters.drivers.guangya.GUANGYA_DOWNLOAD_UA`），用它换链后
交给光鸭「离线下载」→ 数据走 ``115 CDN → 光鸭服务器``，不占本地磁盘/带宽。

**流式**：不预先跑一遍完整目录树。生产者用 ``iter_tree_by_cid`` 实时爬，
爬到一个合格文件就塞进有界队列；N 个消费者并发处理。所以：

- 115 的 1s/目录限速被光鸭下载时间掩盖，不构成瓶颈
- 开跑时**不知道总数**，进度是「已发现 / 已完成」而不是百分比

**幂等**（支持重复执行）：提交前先查光鸭目标目录，同名 + 同大小 →
`skipped_existing`。目标目录就是唯一真相源，不需要任何状态表。

**中断恢复**：上一轮提交的任务可能还在光鸭服务端下载，目标目录里还没有
文件。故启动时拉一次活跃任务列表，从 ``res`` 链接的 ``d=`` 参数里解析
115 pickcode，命中则记 `skipped_inflight`（不提交、不等待）——下一轮由
目标目录预检自然收尾。

**只看状态**：任务结束 = 从活跃列表消失（``cloud_task_list`` 默认不含
完成态，这里只拉 0/1）。不设任何超时——`get_task_status(taskId)` 对在途
任务返回 ``code 145 任务不存在``，不可用；卡死的任务只能靠页面「取消」。
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.adapters.drivers.base import CloudFile, DriverError
from app.adapters.drivers.guangya import GUANGYA_DOWNLOAD_UA
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import session_scope
from app.services import naming
from app.services.driver_factory import build_driver

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.services.migration_jobs import MigrationJob

logger = get_logger(__name__)

#: 轮询离线任务状态的间隔（秒）。
#:
#: 一个共享 poller 服务所有并发 worker——不管开多少并发，都是每 N 秒一次
#: 请求，不会因为并发数把光鸭打爆（限速 0.2s/次）。
#:
#: 实际取值来自配置 ``MIGRATION_POLL_INTERVAL_SEC``（默认 5 秒）；这里是
#: **兜底默认值**，配置读不到时用它。
POLL_INTERVAL_SEC = 5.0

#: 「心跳」日志的间隔（秒）——超过这么久没有新日志就记一条当前在途状态。
#:
#: 大文件下载要几分钟，只有里程碑（25/50/75%）的话中间会长时间没动静，
#: 看起来像卡死。心跳让页面始终有「活着」的证据。
HEARTBEAT_INTERVAL_SEC = 30.0

#: 默认的「提交间隔」（秒）——两次给光鸭提交离线任务之间至少隔这么久。
#:
#: **为什么需要**：115 对「单位时间内拉取不同文件」有频控。实测并发提交时
#: 直链会返回 ``403 {"message":"115 pmt user 11-10"}``（数字随请求次数
#: 递增，末尾固定 ``-10``），光鸭拉不到就记「云添加失败 code=8」。
#:
#: 错开提交是**唯一可靠的对策**——重试只会再撞一次。
DEFAULT_SUBMIT_INTERVAL_SEC = 60.0

#: 任务从活跃列表「消失」几次才认定结束。
#:
#: 刚提交的任务可能还没出现在列表里，一次缺席就判结束会误杀。
ABSENT_CONFIRM = 2

#: 任务结束后校验落盘的重试次数与间隔。
#:
#: 文件索引有延迟：刚完成时目录里可能还查不到，验早了会误报失败。
VERIFY_RETRIES = 3
VERIFY_DELAY_SEC = 5.0

#: 扩展名黑名单（原盘结构，Emby 也读不好）
DISC_EXTS = frozenset({"iso", "m2ts"})

#: 原盘目录名（小写）
DISC_DIRS = frozenset({"bdmv", "certificate"})

#: 隐藏备份文件的名字特征（115 客户端的自动备份残留，如
#: ``.mp-backup-xxxx**..mp4``）。
#:
#: 这些不是正片——实测「没关系，是爱情啊」目录里有 16 个、共 28 GB。
#: 已从 115 源端删除，但**其他目录可能还有**，故迁移时一律跳过。
#:
#: 判定规则与扫描侧共用 :func:`app.services.naming.is_hidden_backup`。
HIDDEN_BACKUP_PATTERNS = naming.HIDDEN_BACKUP_PATTERNS

#: 失败/冲突明细最多保留多少条（避免跑几万个文件时把内存吃满）
MAX_DETAILS = 100

#: 过滤器返回的「被丢弃」原因
FILTER_SMALL = "small"
FILTER_LARGE = "large"
FILTER_EXT = "ext"


class MigrationError(RuntimeError):
    """迁移无法开始（配置错误 / 驱动不可用）。"""


# --------------------------------------------------------------------------
# 目标目录索引（光鸭侧）
# --------------------------------------------------------------------------


class _TargetIndex:
    """目标目录的 fileId + 文件清单缓存。

    一个 run 内共享：同一部影片目录会被多个文件命中，每个目录只查一次。
    并发 worker 共用一个锁——目录解析是低频操作（每个目录一次），串行
    反而避免了「两个 worker 同时给同一目录建副本」的竞态。
    """

    def __init__(self, driver: Any, root_id: str) -> None:
        self._driver = driver
        self._root = str(root_id or "")
        self._dir_ids: dict[str, str] = {"": self._root}
        self._files: dict[str, dict[str, int]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, rel_dir: str) -> tuple[str, dict[str, int]]:
        """解析相对目录，返回 ``(fileId, {文件名: 大小})``（不存在就建）。

        返回的是缓存的**副本**——调用方拿到后随便改都不会污染缓存。
        """
        rel = _norm_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            files = self._files.get(rel)
            if files is None:
                files = dict(await self._driver.list_files(dir_id))
                self._files[rel] = files
            return dir_id, dict(files)

    async def create_offline_task(self, url: str, dir_id: str) -> str:
        """提交离线下载任务（转发给驱动，便于调用方只依赖 index）。"""
        result: str = await self._driver.create_offline_task(url, dir_id)
        return result

    async def note_submitted(self, rel_dir: str, name: str, size: int) -> None:
        """登记「已提交待下载」的文件。

        **必须更新缓存**——否则后续对同一目录的预检会看不到刚提交的文件，
        缓存与光鸭实际状态不一致（``resolve`` 返回旧数据、``refresh``
        返回新数据，两者矛盾）。将来支持「重试失败项」时还会真的重复提交。
        """
        rel = _norm_dir(rel_dir)
        async with self._lock:
            files = self._files.setdefault(rel, {})
            files[name] = size

    async def failure_reason(self, task_id: str) -> str:
        """查光鸭给出的失败原因（查不到就返回空串）。

        只在「任务已结束但目标目录没有文件」时调用——正常路径不额外花请求。
        """
        try:
            items = await self._driver.list_failed_offline_tasks()
        except Exception:
            return ""
        for item in items:
            if str(item.get("taskId")) != task_id:
                continue
            message = str(item.get("errMsg") or "").strip()
            code = item.get("errCode")
            if not message:
                continue
            return f"光鸭离线下载失败：{message}" + (
                f"（code={code}）" if code is not None else ""
            )
        return ""

    async def refresh(self, rel_dir: str) -> dict[str, int]:
        """**重新**列目录（任务结束后校验落盘用，绕过缓存）。"""
        rel = _norm_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            files: dict[str, int] = dict(await self._driver.list_files(dir_id))
            self._files[rel] = files
            return dict(files)

    async def _resolve_dir(self, rel: str) -> str:
        cached = self._dir_ids.get(rel)
        if cached is not None:
            return cached

        parent_rel = posixpath.dirname(rel)
        name = posixpath.basename(rel)
        parent_id = await self._resolve_dir(parent_rel)
        dir_id, created = await self._driver.create_dir(name, parent_id)
        dir_id = str(dir_id)
        self._dir_ids[rel] = dir_id
        if created:
            # 新建目录必定为空，省掉一次列表请求
            self._files[rel] = {}
        return dir_id


# --------------------------------------------------------------------------
# 离线任务监视器
# --------------------------------------------------------------------------


class _TaskWatcher:
    """共享的离线任务轮询器（一个 run 一个）。

    只判断「任务还在不在活跃列表里」——不读 progress 判定完成、不设超时。

    但**会把 progress 报给页面**（实时日志 + 在途进度条）——光鸭的
    ``progress`` 字段是现成的实时进度，不用它展示给用户很浪费。
    """

    def __init__(
        self, driver: Any, job: MigrationJob, poll_interval: float | None = None
    ) -> None:
        self._driver = driver
        self._job = job
        self._pending: dict[str, asyncio.Event] = {}
        self._seen: set[str] = set()
        self._absent: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopped: bool = False
        self._last_heartbeat = time.monotonic()
        if poll_interval is None:
            from app.core.config import get_settings

            poll_interval = get_settings().migration_poll_interval_sec
        self._poll_interval = max(1.0, float(poll_interval))

    async def wait(self, task_id: str) -> None:
        """等一个任务结束（从活跃列表消失）。"""
        event = asyncio.Event()
        self._pending[task_id] = event
        self._ensure_running()
        try:
            await event.wait()
        finally:
            self._pending.pop(task_id, None)
            self._seen.discard(task_id)
            self._absent.pop(task_id, None)

    def _ensure_running(self) -> None:
        if self._stopped:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stopped and self._pending:
            await asyncio.sleep(self._poll_interval)
            try:
                items = await self._driver.list_active_offline_tasks()
            except Exception as exc:  # 轮询失败不致命，下一轮再来
                self._job.log(f"⚠️ 查询离线任务失败（下轮重试）：{exc}")
                continue

            active: set[str] = set()
            for item in items:
                task_id = str(item.get("taskId") or "")
                if not task_id:
                    continue
                active.add(task_id)
                # 报进度（只报**本轮提交**的任务，遗留任务不占日志）
                if task_id in self._pending:
                    raw = item.get("progress")
                    self._job.update_task_progress(
                        task_id,
                        None if raw is None else int(raw),
                    )

            for task_id, event in list(self._pending.items()):
                if task_id in active:
                    self._seen.add(task_id)
                    self._absent[task_id] = 0
                    continue
                self._absent[task_id] = self._absent.get(task_id, 0) + 1
                if task_id in self._seen or self._absent[task_id] >= ABSENT_CONFIRM:
                    event.set()

            self._maybe_heartbeat()

    async def close(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _maybe_heartbeat(self) -> None:
        """长时间没有新日志时，记一条当前在途状态（证明还活着）。

        **每个任务一行**——早先把多个任务挤在一行，结果日志框里换行错乱、
        完全看不出哪个是哪个（截图里就是这样）。且用**完整路径**，只看文件名
        分不清是哪部剧。
        """
        now = time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SEC:
            return
        self._last_heartbeat = now
        items = self._job.in_flight_items
        if not items:
            return
        self._job.log(f"⏱️ 仍在下载（{len(items)} 个）：")
        for item in items.values():
            # 用「│」做缩进标记——不能用空格（前端剥时间戳时会把前导空白吃掉）
            self._job.log(
                f"│ {item['path']}　{_progress_text(item['progress'])}"
            )


class _SubmitGate:
    """全局提交闸门：保证两次提交之间至少间隔 N 秒。

    **为什么要全局**（而不是每个 worker 各自 sleep）：worker 是并发的，
    各自 sleep 仍可能撞在同一时刻。闸门用一把锁串行化「排队 + 计时」，
    保证实际提交时刻彼此至少相隔 ``interval``。

    **先过闸门再换链**：直链是「新鲜的」，光鸭收到就能开始拉；
    若是先换链再等，链接可能在队列里放几十秒才开始下载。
    """

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, float(interval))
        self._next_at: float = 0.0
        self._lock = asyncio.Lock()
        #: 累计等待秒数（给日志/统计用）
        self.total_waited = 0.0

    async def wait(self) -> float:
        """等到可以提交为止，返回本次实际等待的秒数。"""
        if self._interval <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay > 0:
                await asyncio.sleep(delay)
            # 等待结束后才推进「下次可提交时刻」
            self._next_at = time.monotonic() + self._interval
            self.total_waited += delay
            return delay


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


async def run_migration(job: MigrationJob) -> None:
    """跑一轮迁移（由 :mod:`app.services.migration_jobs` 在后台调用）。"""
    params = job.params

    async with session_scope() as db:
        source = (
            await db.execute(select(Driver).where(Driver.id == params.source_driver_id))
        ).scalar_one_or_none()
        target = (
            await db.execute(select(Driver).where(Driver.id == params.target_driver_id))
        ).scalar_one_or_none()

    if source is None or target is None:
        raise MigrationError("存储源不存在")
    if source.type != "p115":
        raise MigrationError(f"源存储源必须是 115（当前：{source.type}）")
    if target.type != "guangya":
        raise MigrationError(f"目标存储源必须是光鸭盘（当前：{target.type}）")

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(params.target_driver_id, cred)

    p115 = build_driver("p115", source.credential, source.device_type)
    gyp = build_driver(
        "guangya",
        target.credential,
        target.device_type,
        on_token_refresh=_on_refresh,
    )

    # 源目录路径：以 cid 反解的路径为准（前端传来的路径可能过期）
    base_path = (params.source_path or "/").rstrip("/") or "/"
    resolver = getattr(p115, "resolve_path", None)
    if resolver is not None:
        try:
            resolved = await resolver(params.source_cid)
            if resolved:
                base_path = str(resolved).rstrip("/") or "/"
        except Exception as exc:
            job.log(f"⚠️ 源目录路径解析失败，沿用前端传入的路径：{exc}")
    job.source_path = base_path

    # 上一轮遗留的在途任务（中断恢复）
    inflight = await _collect_inflight(gyp, job)
    job.legacy_inflight = len(inflight)

    video_exts = {
        e.strip().lower() for e in (source.video_exts or "").split(",") if e.strip()
    }
    min_bytes = max(params.min_size_mb, 0) * 1024 * 1024
    max_bytes = max(params.max_size_mb, 0) * 1024 * 1024

    job.log(
        f"源 {base_path}（cid={params.source_cid}）→ 目标 {params.target_path or '/'}"
        f"；阈值 ≥{params.min_size_mb} MB，并发 {params.concurrency}"
        f"，提交间隔 {params.submit_interval_sec:g}s"
    )
    if inflight:
        job.log(
            f"检测到 {len(inflight)} 个上一轮遗留的在途任务，本轮跳过对应文件"
            f"（实际光鸭并发 ≈ {len(inflight) + params.concurrency}）"
        )
        if len(inflight) >= 5:
            job.log(
                "⚠️ 遗留任务较多——它们同样在消耗 115 的下载频控配额，"
                "建议等它们跑完再开新任务（否则更容易撞上「云添加失败 code=8」）"
            )

    queue: asyncio.Queue[CloudFile] = asyncio.Queue(
        maxsize=max(params.concurrency * 2, 4)
    )
    index = _TargetIndex(gyp, params.target_file_id)
    watcher = _TaskWatcher(gyp, job)
    gate = _SubmitGate(params.submit_interval_sec)

    producer: asyncio.Task[None] | None = None
    workers: list[asyncio.Task[None]] = []
    try:
        producer = asyncio.create_task(
            _produce(
                job,
                p115,
                queue,
                base_path=base_path,
                video_exts=video_exts,
                min_bytes=min_bytes,
                max_bytes=max_bytes,
                exclude_disc=params.exclude_disc,
            )
        )
        workers = [
            asyncio.create_task(
                _worker(
                    job,
                    p115=p115,
                    index=index,
                    watcher=watcher,
                    gate=gate,
                    queue=queue,
                    inflight=inflight,
                    base_path=base_path,
                )
            )
            for _ in range(params.concurrency)
        ]

        await producer
        job.log(
            f"目录遍历结束：{job.dirs_walked} 个目录，{job.discovered} 个待迁移文件"
            f"（过滤：阈值 {job.filtered_small} / 超上限 {job.filtered_large} / "
            f"后缀或原盘 {job.filtered_ext}）"
        )
        await queue.join()
    finally:
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if producer is not None and not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        await watcher.close()
        job.in_flight = 0


# --------------------------------------------------------------------------
# 生产者 / 消费者
# --------------------------------------------------------------------------


async def _produce(
    job: MigrationJob,
    driver: Any,
    queue: asyncio.Queue[CloudFile],
    *,
    base_path: str,
    video_exts: set[str],
    min_bytes: int,
    max_bytes: int,
    exclude_disc: bool,
) -> None:
    """实时爬 115 目录树，把合格文件塞进队列。"""
    dirs: set[str] = set()

    async for entry in driver.iter_tree_by_cid(
        job.params.source_cid, parent_path=base_path, include_subdirs=True
    ):
        if job.state != "running":
            return

        parent = posixpath.dirname(entry.path)
        if parent not in dirs:
            dirs.add(parent)
            job.dirs_walked = len(dirs)

        reason = _filter(
            entry,
            video_exts=video_exts,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            exclude_disc=exclude_disc,
        )
        if reason is not None:
            job.count_filtered(reason)
            continue

        job.discovered += 1
        await queue.put(entry)


async def _worker(
    job: MigrationJob,
    *,
    p115: Any,
    index: _TargetIndex,
    watcher: _TaskWatcher,
    gate: _SubmitGate,
    queue: asyncio.Queue[CloudFile],
    inflight: set[str],
    base_path: str,
) -> None:
    """消费队列：一个文件失败不影响其它文件。"""
    while True:
        entry = await queue.get()
        try:
            if job.state != "running":
                continue
            await _migrate_one(
                job,
                entry,
                p115=p115,
                index=index,
                watcher=watcher,
                gate=gate,
                inflight=inflight,
                base_path=base_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 任何异常 = 跳过这个文件
            job.record_failed(
                entry, f"{type(exc).__name__}: {exc}"
            )
            logger.warning("迁移失败 %s：%s", entry.path, exc)
        finally:
            queue.task_done()


async def _migrate_one(
    job: MigrationJob,
    entry: CloudFile,
    *,
    p115: Any,
    index: _TargetIndex,
    watcher: _TaskWatcher,
    gate: _SubmitGate,
    inflight: set[str],
    base_path: str,
) -> None:
    """处理单个文件：预检 → 换链 → 提交 → 等结束 → 校验落盘。"""
    job.current_path = entry.path
    rel_dir = _relative_dir(entry.path, base_path)

    target_dir_id, existing = await index.resolve(rel_dir)

    # ① 目标已有：同名同大小 → 跳过；同名不同大小 → 冲突（不覆盖、不重传）
    remote_size = existing.get(entry.name)
    if remote_size is not None:
        if remote_size == entry.size:
            job.skipped_existing += 1
            job.log(f"⏭️ 已存在，跳过：{entry.path}")
        else:
            job.record_conflict(entry, remote_size)
            job.log(
                f"⚠️ 同名但大小不同，跳过：{entry.path}"
                f"（115 {entry.size:,} / 光鸭 {remote_size:,} 字节）"
            )
        return

    # ② 上一轮还在下载（目标目录里还没有文件）
    if entry.remote_id in inflight:
        job.skipped_inflight += 1
        job.log(f"⏭️ 上一轮仍在下载，本轮跳过：{entry.path}")
        return

    # ③ 错开提交：115 对「短时间拉取不同文件」有频控（实测 403
    #    "115 pmt user 11-10"），光鸭拉不到就记「云添加失败 code=8」。
    #    **先过闸门再换链**——直链是新鲜的，光鸭收到就能开始拉。
    waited = await gate.wait()
    if waited >= 1:
        job.log(f"⏱️ 错开提交，等待 {waited:.0f}s：{entry.path}")

    # ④ 用光鸭下载器的 UA 换 115 直链（f=1 必须匹配，否则 403）
    link = await p115.get_direct_link(entry.remote_id, GUANGYA_DOWNLOAD_UA)
    if not link.url:
        raise DriverError("115 返回空直链", kind="not_found")
    job.log(f"🔗 已换取直链：{entry.path}")

    task_id = await index.create_offline_task(link.url, target_dir_id)
    # 登记到缓存——否则同目录的后续预检看不到这个文件（缓存与实际不一致）
    await index.note_submitted(rel_dir, entry.name, entry.size)
    job.log(
        f"⬆️ 已提交：{entry.path}"
        f"（{entry.size / 1024**3:.2f} GB）"
    )

    job.in_flight += 1
    job.track_task(task_id, entry.path, entry.size)
    try:
        await watcher.wait(task_id)
    finally:
        job.in_flight -= 1
        job.untrack_task(task_id)

    # ④ 任务结束 → 以「目标目录里是否真的出现同名同大小文件」为准
    #    索引有延迟，验早了会误报，故重试几次
    for attempt in range(VERIFY_RETRIES):
        files = await index.refresh(rel_dir)
        if files.get(entry.name) == entry.size:
            job.success += 1
            job.migrated_bytes += entry.size
            job.log(f"✅ 完成：{entry.path}")
            return
        if attempt + 1 < VERIFY_RETRIES:
            await asyncio.sleep(VERIFY_DELAY_SEC)

    reason = await index.failure_reason(task_id)
    job.record_failed(
        entry, reason or "任务已结束但目标目录未出现同名同大小文件"
    )


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _filter(
    entry: CloudFile,
    *,
    video_exts: set[str],
    min_bytes: int,
    max_bytes: int,
    exclude_disc: bool,
) -> str | None:
    """判断是否该丢弃，返回原因（``None`` = 保留）。"""
    name = entry.name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    # 隐藏备份文件（115 客户端残留）——一律跳过，不是正片
    if naming.is_hidden_backup(name):
        return FILTER_EXT

    if video_exts and ext not in video_exts:
        return FILTER_EXT

    if exclude_disc:
        if ext in DISC_EXTS:
            return FILTER_EXT
        parts = {p.lower() for p in entry.path.split("/") if p}
        if parts & DISC_DIRS:
            return FILTER_EXT

    if min_bytes > 0 and entry.size < min_bytes:
        return FILTER_SMALL
    if max_bytes > 0 and entry.size > max_bytes:
        return FILTER_LARGE
    return None


def _relative_dir(path: str, base_path: str) -> str:
    """源文件路径 → 目标下的相对目录。

    语义：**源目录 A 的内容原样铺到目标目录 B 下**，A 本身不出现
    （A 与 B 同级）。

    例：``/影视/电影/动画电影/龙猫 (1988)/x.mkv`` + A=``/影视/电影/动画电影``
    → ``龙猫 (1988)``（落在 B 下的 ``龙猫 (1988)/x.mkv``）。

    A 选在片名层时同理：A=``/影视/电视剧/国产剧/三体 (2023)`` →
    ``Season 1``。
    """
    parent = posixpath.dirname(path.rstrip("/"))
    base = base_path.rstrip("/")
    if not base or base == "/":
        return _norm_dir(parent)
    if parent == base:
        return ""
    prefix = f"{base}/"
    if parent.startswith(prefix):
        return _norm_dir(parent[len(prefix) :])
    # 路径不在源目录下（cid 与后端反解的路径不一致）→ 落到目标根
    return ""


def _norm_dir(rel: str) -> str:
    return "/".join(p for p in rel.strip("/").split("/") if p)


def _progress_text(progress: int) -> str:
    """进度文案：``-1`` = 光鸭还没开始下载（排队中）。"""
    return "排队中" if progress < 0 else f"{progress}%"


async def _collect_inflight(driver: Any, job: MigrationJob) -> set[str]:
    """收集上一轮遗留的在途任务，返回 115 pickcode 集合。

    任务项在活跃阶段不带 ``fileId`` / ``parentId``，但 ``res`` 字段里的
    115 直链带 ``d=vip-<uid>-<pickcode>-1-0``，据此可以精确匹配回源文件。
    """
    try:
        items = await driver.list_active_offline_tasks()
    except Exception as exc:
        job.log(f"⚠️ 读取在途任务失败，本轮不做中断去重：{exc}")
        return set()

    pickcodes: set[str] = set()
    for item in items:
        pickcode = _pickcode_from_task(item)
        if pickcode:
            pickcodes.add(pickcode)
    return pickcodes


def _pickcode_from_task(item: dict[str, Any]) -> str:
    """从离线任务的 ``res`` 直链里解析 115 pickcode。"""
    url = str(item.get("res") or "")
    if not url:
        return ""
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return ""
    raw = (query.get("d") or [""])[0]
    parts = raw.split("-")
    # d = vip-<uid>-<pickcode>-<x>-<y>
    return parts[2] if len(parts) > 2 else ""


async def persist_guangya_credential(driver_id: int, cred: Any) -> None:
    """光鸭 Token 刷新后写回 DB（不写回则重启后 401）。"""
    from app.adapters.drivers.guangya_credential import SCHEME, GuangyaCredential
    from app.services.driver_service import store_credential

    if not isinstance(cred, GuangyaCredential):
        return
    try:
        async with session_scope() as db:
            await store_credential(
                db,
                driver_id,
                cred.to_json(),
                scheme=SCHEME,
                # Token 刷新是**同一个账号换 token**，登录态没变
                # → 不能作废测连缓存（否则面板退回「未测连」）。
                invalidate_cache=False,
            )
        logger.info("已写回刷新的光鸭凭据 driver_id=%s", driver_id)
    except Exception:
        logger.exception("写回刷新的光鸭凭据失败（本次任务不受影响）")


# ================================================================ 目录浏览
#
# **API 层不碰 DB / 适配层** —— 路由层只调这些（分层约束，见
# ``tests/unit/test_layering.py``）。


class MigrationBrowseError(Exception):
    """目录浏览失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


async def _load_driver_row(driver_id: int, expected_type: str) -> Any:
    """取存储源行并校验类型。

    Raises:
        MigrationBrowseError: 存储源不存在（404）/ 类型不符（400）。
    """
    from sqlalchemy import select

    from app.db.models import Driver

    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == driver_id))
        ).scalar_one_or_none()
        if row is None:
            raise MigrationBrowseError(
                f"存储源不存在：{driver_id}", status_code=404, kind="not_found"
            )
        if row.type != expected_type:
            raise MigrationBrowseError(
                f"该接口需要 {expected_type} 存储源（当前：{row.type}）",
                status_code=400,
                kind="config",
            )
        return row


def _default_source_cid(media_cid: str | None) -> str:
    """115 浏览的兜底 cid —— **必须用媒体库 cid**。

    不能用 ``"0"``：那是**115 账号根目录**，实测会列出「手机相册 / 资料 /
    密钥 / 备份」等私人目录（实测输出见下方注释）。存储源没配媒体库 cid 时
    应报错，而不是把账号根目录暴露给迁移页面——用户可能误把私人目录当
    媒体库迁走。

    实测 ``parent_id=0`` 的返回::

        影视 / 手机相册 / 资料 / 密钥 / 备份 / …
    """
    cid = (media_cid or "").strip()
    if not cid:
        raise MigrationBrowseError(
            "该存储源未配置媒体库 cid，请先在「存储源」里配置后再浏览目录",
            status_code=400,
            kind="config",
        )
    return cid


async def _list_by_cid(driver: Any, cid: str) -> list[Any]:
    """按 id 列直接子项。

    ``list_dir_by_cid`` 不在 :class:`CloudDriver` 协议里（115 / 光鸭都有，
    但协议只要求 ``list_dir``），故用 ``getattr`` 取——顺带给出友好报错。
    """
    from app.adapters.drivers.base import DriverError

    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        raise MigrationBrowseError(
            "该存储源驱动不支持按 id 浏览目录", status_code=400, kind="config"
        )
    try:
        entries: list[Any] = await lister(cid)
        return entries
    except DriverError as exc:
        raise MigrationBrowseError(str(exc), status_code=400, kind="upstream") from exc


async def browse_source_dirs(
    driver_id: int, parent_id: str = ""
) -> list[tuple[str, str]]:
    """列出 115 目录下的子目录（按 cid 逐层浏览）。

    返回 ``[(remote_id, name), ...]``——只含目录。
    """
    row = await _load_driver_row(driver_id, "p115")
    cid = parent_id or _default_source_cid(row.media_cid)
    driver = build_driver(row.type, row.credential, row.device_type)
    entries = await _list_by_cid(driver, cid)
    return [(e.remote_id, e.name) for e in entries if e.is_dir]


async def browse_target_dirs(
    driver_id: int, parent_id: str = ""
) -> list[tuple[str, str]]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。

    返回 ``[(remote_id, name), ...]``——只含目录。
    """
    row = await _load_driver_row(driver_id, "guangya")

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(row.id, cred)

    driver = build_driver(
        row.type, row.credential, row.device_type, on_token_refresh=_on_refresh
    )
    entries = await _list_by_cid(driver, parent_id or "")
    return [(e.remote_id, e.name) for e in entries if e.is_dir]


async def validate_migration_drivers(
    source_driver_id: int, target_driver_id: int
) -> None:
    """校验迁移的源 / 目标存储源（类型必须对）。

    Raises:
        MigrationBrowseError: 存储源不存在（404）/ 类型不符（400）。
    """
    await _load_driver_row(source_driver_id, "p115")
    await _load_driver_row(target_driver_id, "guangya")

