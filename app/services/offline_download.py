"""离线下载（本地文件 → 光鸭）：把本地文件交给**光鸭服务端**去拉。

**为什么有它**：数据同步是「128 → 光鸭 OSS」的**上行**——实测受本机上行
带宽限制（~3.3 MB/s，几百 GB 要数十小时，见 ``docs/upload-bandwidth.md``）。

光鸭的**离线下载**是反过来：光鸭服务器拿着 URL **主动来拉**。流量方向
变成**下行**，本机只承担接收 → **上行需求 ≈ 0**。

本模块负责：
- 遍历本地目录，挑出该传的文件
- 为每个文件生成带 HMAC token 的公网 URL
- 提交离线任务给光鸭、轮询直到结束、校验文件真的落地

**与「数据同步」完全独立**：那个走 ``UploadDriver`` 协议、同步语义、逐文件
上传；这里走光鸭的离线任务、异步语义、后台跑。**两者不共用代码路径**。

**与「迁移」（115→光鸭）同构**：那也是「提交离线任务 → 轮询 → 校验」，
所以这里的任务模型、轮询器、闸门都照搬 ``app.services.migration``。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.services.offline_files import build_public_url

logger = get_logger(__name__)

#: 原盘扩展名（不传——太大且没意义）
DISC_EXTS = frozenset({"iso", "m2ts"})

#: 原盘目录名（小写）
DISC_DIRS = frozenset({"bdmv", "certificate"})

#: 校验落盘时的重试次数 / 间隔（秒）。
#:
#: 光鸭的目录索引有延迟——任务刚结束时文件可能还查不到，验早了会误报
#: 「失败」。与迁移功能同参数。
VERIFY_RETRIES = 5
VERIFY_DELAY_SEC = 2.0


class OfflineDownloadError(RuntimeError):
    """离线下载的入参 / 配置错误。"""


@dataclass
class OfflineFile:
    """一个待传文件。"""

    #: 相对根目录的路径（用 ``/`` 分隔）——**也是 token 的输入**
    rel_path: str
    #: 绝对路径
    abs_path: Path
    #: 字节数
    size: int

    @property
    def name(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1]

    @property
    def parent_rel(self) -> str:
        head = self.rel_path.rsplit("/", 1)[0] if "/" in self.rel_path else ""
        return head


@dataclass
class OfflineParams:
    """一次离线下载的入参。"""

    #: 目标光鸭 driver id
    target_driver_id: int
    #: 目标光鸭目录 fileId（空 = 根目录）
    target_file_id: str = ""
    #: 目标路径（只读展示）
    target_path: str = ""
    #: 本地根目录（容器内绝对路径）
    local_root: str = ""
    #: 递归子目录
    include_subdirs: bool = True
    #: 体积下限（MB），0 = 不过滤
    min_size_mb: int = 0
    #: 体积上限（MB），0 = 不限
    max_size_mb: int = 0
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #:
    #: **只要这些扩展名**（小写，不含点）。空 = 不限（全传）。
    #: 如 ``("mkv", "mp4")`` 只传视频，跳过 nfo / 字幕 / 图片。
    exts: tuple[str, ...] = ()
    #:
    #: **下载成功后删除本地文件**（不可逆）。
    #:
    #: 破坏性操作——三道保护与「数据同步」一致（见
    #: :func:`_maybe_delete_local`）：只在「云端确有同名同大小文件」
    #: 时删、路径安全（普通文件 / 非隐藏 / 在源根目录内）、空目录一并删。
    delete_after_upload: bool = False
    #: 同时进行的离线任务数
    concurrency: int = 2
    #: 两次提交之间的最小间隔（秒）。**没有 115 频控，可以很小**
    submit_interval_sec: float = 2.0
    #: 公网入口（如 ``http://home.banstack.top:8092``）
    public_base: str = ""
    #: HMAC 密钥
    secret: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_driver_id": self.target_driver_id,
            "target_file_id": self.target_file_id,
            "target_path": self.target_path,
            "local_root": self.local_root,
            "include_subdirs": self.include_subdirs,
            "min_size_mb": self.min_size_mb,
            "max_size_mb": self.max_size_mb,
            "exclude_disc": self.exclude_disc,
            "exts": list(self.exts),
            "delete_after_upload": self.delete_after_upload,
            "concurrency": self.concurrency,
            "submit_interval_sec": self.submit_interval_sec,
            "public_base": self.public_base,
        }


def build_url(params: OfflineParams, rel_path: str) -> str:
    """给某个相对路径拼出公网 URL（含 token）。

    纯逻辑在 :func:`app.services.offline_files.build_public_url`。
    """
    return build_public_url(params.public_base, params.secret, rel_path)


def iter_files(
    root: Path,
    *,
    include_subdirs: bool,
    min_bytes: int,
    max_bytes: int,
    exclude_disc: bool,
    exts: set[str] | None = None,
) -> Iterator[OfflineFile]:
    """逐个**产出**待传文件（流式）。

    与全量扫描的区别：调用方拿到**第一个**文件就可以开始处理，不必等
    整棵树走完——「边扫描边提交」的基础。目录按名字序遍历，顺序确定。

    **不跟随符号链接**——避免环，也避免把不该暴露的东西带出去。

    Args:
        exts: **只要这些扩展名**（小写，不含点）。``None`` / 空集 = 不限
            （全传）。如 ``{"mkv", "mp4"}`` 只传视频。
    """
    base = root.resolve()
    wanted = {e.lower().lstrip(".") for e in (exts or set())}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(base)
        rel_dir_s = "" if str(rel_dir) == "." else rel_dir.as_posix()

        if exclude_disc and rel_dir_s:
            parts = {p.lower() for p in rel_dir_s.split("/") if p}
            if parts & DISC_DIRS:
                dirnames[:] = []
                continue

        dirnames.sort()  # 确定性顺序（os.walk 本身不保证）

        for fname in filenames:
            fpath = current / fname
            try:
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                size = fpath.stat().st_size
            except OSError:
                continue

            if size == 0:
                continue
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if exclude_disc and ext in DISC_EXTS:
                continue
            if wanted and ext not in wanted:
                continue
            if min_bytes > 0 and size < min_bytes:
                continue
            if max_bytes > 0 and size > max_bytes:
                continue

            rel = f"{rel_dir_s}/{fname}" if rel_dir_s else fname
            yield OfflineFile(rel_path=rel, abs_path=fpath, size=size)

        if not include_subdirs:
            dirnames[:] = []


def collect_files(
    root: Path,
    *,
    include_subdirs: bool,
    min_bytes: int,
    max_bytes: int,
    exclude_disc: bool,
    exts: set[str] | None = None,
) -> list[OfflineFile]:
    """全量扫描并按路径排序（兼容入口）。

    流式版本见 :func:`iter_files`——任务执行走那边（边扫边提交），
    本函数留给测试与一次性盘点。
    """
    return sorted(
        iter_files(
            root,
            include_subdirs=include_subdirs,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            exclude_disc=exclude_disc,
            exts=exts,
        ),
        key=lambda f: f.rel_path,
    )


@dataclass
class OfflineJob:
    """一次离线下载任务的运行状态。"""

    id: str
    params: OfflineParams
    seq: int = 0
    state: str = "running"
    started_at: int = field(default_factory=lambda: int(time.time()))
    finished_at: int | None = None

    #: 当前处理的文件（展示用）
    current_path: str = ""
    #: 已发现的文件数
    discovered: int = 0
    #: 已提交、仍在光鸭上跑的任务数
    in_flight: int = 0
    #: 在途任务明细：``taskId → {名称, 进度%, 大小}``
    in_flight_items: dict[str, dict[str, Any]] = field(default_factory=dict)

    success: int = 0
    failed: int = 0
    skipped_existing: int = 0
    conflicts: int = 0
    #: 已成功下载的字节数
    downloaded_bytes: int = 0
    #: 已删除的本地文件数（``delete_after_upload`` 开启时）
    local_deleted: int = 0

    failed_items: list[dict[str, Any]] = field(default_factory=list)
    conflict_items: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    #: 后台 task（不参与序列化）
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def done(self) -> bool:
        """任务是否已结束（不在跑）。"""
        return self.state != "running"

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > 500:
            del self.logs[: len(self.logs) - 500]

    def record_failed(self, item: OfflineFile, reason: str) -> None:
        self.failed += 1
        self.failed_items.append(
            {"path": item.rel_path, "size": item.size, "reason": reason}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_driver_id": self.params.target_driver_id,
            "target_file_id": self.params.target_file_id,
            "target_path": self.params.target_path,
            "local_root": self.params.local_root,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": (self.finished_at or int(time.time())) - self.started_at,
            "current_path": self.current_path,
            "discovered": self.discovered,
            "in_flight": self.in_flight,
            "in_flight_items": self.in_flight_items,
            "success": self.success,
            "failed": self.failed,
            "skipped_existing": self.skipped_existing,
            "conflicts": self.conflicts,
            "downloaded_bytes": self.downloaded_bytes,
            "local_deleted": self.local_deleted,
            "failed_items": self.failed_items[:100],
            "conflict_items": self.conflict_items[:100],
            "logs": self.logs[-500:],
            "error": self.error,
        }


class _SubmitGate:
    """全局提交闸门：保证两次提交之间至少间隔 N 秒。"""

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, float(interval))
        self._next_at: float = 0.0
        self._lock = asyncio.Lock()
        self.total_waited = 0.0

    async def wait(self) -> float:
        if self._interval <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = time.monotonic() + self._interval
            self.total_waited += delay
            return delay


class _TaskWatcher:
    """离线任务轮询器（一个 run 一个）。

    只判断「任务还在不在活跃列表里」——不读 progress 判定完成、不设超时。

    ⚠️ **光鸭的 ``progress`` 字段不是可靠的百分比**（实测 2026-11-04）：
    5MB 小文件时 37→95→97（像百分比），但 2.3GB 文件下载到 88% 时
    ``progress`` 只有 4。**故不用它判定完成，也别当百分比展示**——
    只原样透出给前端做「光鸭侧计数」参考。

    完成判据是「目标目录出现同名同大小文件」（见 ``_process_one``）。
    """

    def __init__(self, driver: Any, job: OfflineJob, poll_interval: float = 5.0) -> None:
        self._driver = driver
        self._job = job
        self._pending: dict[str, asyncio.Event] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._poll_interval = max(1.0, float(poll_interval))

    async def wait(self, task_id: str) -> None:
        event = asyncio.Event()
        self._pending[task_id] = event
        self._ensure_running()
        try:
            await event.wait()
        finally:
            self._pending.pop(task_id, None)

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
            except Exception as exc:  # 轮询失败不终止任务
                logger.warning("读活跃离线任务失败：%s", exc)
                continue
            active_ids = {str(it.get("taskId")) for it in items}
            for item in items:
                tid = str(item.get("taskId"))
                if tid in self._pending:
                    self._job.in_flight_items[tid] = {
                        "name": str(item.get("name") or ""),
                        "progress": item.get("progress"),
                        "size": item.get("size"),
                    }
            for tid in list(self._pending):
                if tid not in active_ids:
                    self._pending[tid].set()
        self._stopped = True

    def stop(self) -> None:
        self._stopped = True


async def _load_target_driver(driver_id: int) -> Any:
    """取光鸭驱动（含 Token 刷新的写回回调）。"""
    from sqlalchemy import select

    from app.db.models import Driver
    from app.db.session import session_scope
    from app.services.driver_factory import build_driver
    from app.services.migration import persist_guangya_credential

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Driver).where(Driver.id == driver_id, Driver.type == "guangya")
            )
        ).scalar_one_or_none()
        if row is None:
            raise OfflineDownloadError("目标存储源不存在或不是光鸭")
        if not row.enabled:
            raise OfflineDownloadError("目标存储源已停用")
        if not row.credential:
            raise OfflineDownloadError("目标存储源没有凭据")
        cred = row.credential
        driver_type = row.type
        device_type = row.device_type

    async def _on_refresh(new_cred: Any) -> None:
        await persist_guangya_credential(driver_id, new_cred)

    return build_driver(
        driver_type, cred, device_type, on_token_refresh=_on_refresh
    )


async def _resolve_target_dir(
    driver: Any, file_id: str, rel_dir: str, cache: dict[str, str] | None = None
) -> str:
    """在光鸭上定位/创建目标目录，返回 fileId。

    ``ensure_dir`` 的契约是**返回最终 fileId 字符串**（它自己逐层建，
    目录已存在时幂等返回现有 id）——不是元组。⚠️ 曾经写成
    ``current, _ = await driver.ensure_dir(...)``：对字符串按字符解包，
    fileId 长度不是 2 就抛 ``too many values to unpack``——且只有
    **子目录里的文件**才走这段，根目录文件正常，问题被掩盖到 prod。

    Args:
        cache: ``rel_dir → fileId`` 缓存（同一次任务里同一目录只建一次）。
    """
    if not rel_dir:
        return file_id
    if cache is not None and rel_dir in cache:
        return cache[rel_dir]
    resolved: str = await driver.ensure_dir(rel_dir, file_id)
    if cache is not None:
        cache[rel_dir] = resolved
    return resolved


#: 队列哨兵：扫描线程用它告诉所有工作协程「没有更多文件了」
_WALK_DONE = object()


async def run_offline_download(job: OfflineJob) -> None:
    """跑一次离线下载（后台任务的主体）。

    **扫描与提交是流水线**（2026-11-04 起）：扫描在**工作线程**里走目录
    树（CIFS 的 readdir / stat 是阻塞 IO，不能占事件循环），每找到一个
    文件就丢进队列，工作协程**立刻**提交——第一个文件的提交在扫描开始后
    毫秒级就发生，不用等整棵树扫完。「已发现」计数也随扫描实时增长。
    """
    params = job.params
    driver = await _load_target_driver(params.target_driver_id)

    root = Path(params.local_root)
    if not root.is_dir():
        job.error = f"本地目录不存在：{params.local_root}"
        job.state = "failed"
        job.finished_at = int(time.time())
        return

    suffix = f"（仅 {', '.join(params.exts)}）" if params.exts else ""
    job.log(f"📂 边扫描边提交：{root}{suffix}")

    watcher = _TaskWatcher(driver, job)
    gate = _SubmitGate(params.submit_interval_sec)
    concurrency = max(1, params.concurrency)
    sem = asyncio.Semaphore(concurrency)
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    workers_left = concurrency  # 还有多少工作协程没消费过哨兵
    dir_cache: dict[str, str] = {}  # rel_dir → fileId（同一目录只 ensure 一次）

    def _walk() -> None:
        """扫描线程：走目录树，逐个把文件丢进队列。**必须**以哨兵收尾。

        ``discovered`` 由本线程**唯一**写入（worker 不碰）——单写者无竞态，
        前端轮询看到的是「已扫到多少」的实时总数，而不是「已处理多少」。
        """
        try:
            for item in iter_files(
                root,
                include_subdirs=params.include_subdirs,
                min_bytes=params.min_size_mb * 1024 * 1024,
                max_bytes=params.max_size_mb * 1024 * 1024,
                exclude_disc=params.exclude_disc,
                exts=set(params.exts),
            ):
                job.discovered += 1
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except Exception as exc:
            job.log(f"❌ 扫描中断：{type(exc).__name__}: {exc}")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _WALK_DONE)

    producer = asyncio.create_task(asyncio.to_thread(_walk))

    async def worker() -> None:
        nonlocal workers_left
        while True:
            item = await queue.get()
            if item is _WALK_DONE:
                queue.task_done()
                workers_left -= 1
                if workers_left > 0:
                    # 把哨兵传给下一个工作协程；最后一个消费它时不再回填，
                    # 队列的 unfinished 计数才能归零（join() 返回的前提）。
                    queue.put_nowait(_WALK_DONE)
                return
            try:
                await _process_one(
                    job,
                    item,
                    driver=driver,
                    watcher=watcher,
                    gate=gate,
                    sem=sem,
                    dir_cache=dir_cache,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job.record_failed(item, f"{type(exc).__name__}: {exc}")
                job.log(f"❌ 失败：{item.rel_path}（{exc}）")
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    try:
        await producer
        await queue.join()
    finally:
        # 正常结束时工作协程都已返回，cancel() 是 no-op；
        # 异常 / 取消路径上它负责收掉还挂在 queue.get() 上的协程。
        watcher.stop()
        for w in workers:
            w.cancel()

    if job.discovered == 0:
        job.state = "success"
        job.finished_at = int(time.time())
        job.log("✅ 没有需要传的文件")
        return

    job.state = "success" if job.failed == 0 else "failed"
    job.finished_at = int(time.time())
    job.log(
        f"🏁 结束：发现 {job.discovered}，成功 {job.success}，失败 {job.failed}，"
        f"跳过已存在 {job.skipped_existing}，冲突 {job.conflicts}"
    )


async def _process_one(
    job: OfflineJob,
    item: OfflineFile,
    *,
    driver: Any,
    watcher: _TaskWatcher,
    gate: _SubmitGate,
    sem: asyncio.Semaphore,
    dir_cache: dict[str, str] | None = None,
) -> None:
    """处理单个文件：预检 → 提交 → 等结束 → 校验落盘。"""
    job.current_path = item.rel_path

    # ① 目标目录定位（同一次任务里同一目录只 ensure 一次）
    target_dir_id = await _resolve_target_dir(
        driver, job.params.target_file_id, item.parent_rel, cache=dir_cache
    )

    # ② 预检：同名同大小 → 跳过；同名不同大小 → 冲突
    existing = await driver.list_files(target_dir_id)
    remote_size = existing.get(item.name)
    if remote_size is not None:
        if remote_size == item.size:
            job.skipped_existing += 1
            job.log(f"⏭️ 已存在，跳过：{item.rel_path}")
        else:
            job.conflicts += 1
            job.conflict_items.append(
                {"path": item.rel_path, "local_size": item.size, "remote_size": remote_size}
            )
            job.log(
                f"⚠️ 同名但大小不同，跳过：{item.rel_path}"
                f"（本地 {item.size:,} / 光鸭 {remote_size:,} 字节）"
            )
        return

    # ③ 错开提交（避免光鸭对短时间大量任务限流）
    waited = await gate.wait()
    if waited >= 1:
        job.log(f"⏱️ 错开提交，等待 {waited:.0f}s：{item.rel_path}")

    # ④ 生成带 token 的 URL 并提交
    url = build_url(job.params, item.rel_path)
    async with sem:
        task_id = await driver.create_offline_task(url, target_dir_id)
    job.in_flight += 1
    job.in_flight_items[task_id] = {
        "name": item.name,
        "progress": 0,
        "size": item.size,
    }
    job.log(f"⬆️ 已提交：{item.rel_path}（{item.size / 1024**3:.2f} GB）")

    try:
        await watcher.wait(task_id)
    finally:
        job.in_flight -= 1
        job.in_flight_items.pop(task_id, None)

    # ⑤ 校验落盘：以「目标目录出现同名同大小文件」为准（索引有延迟，重试几次）
    for attempt in range(VERIFY_RETRIES):
        files = await driver.list_files(target_dir_id)
        if files.get(item.name) == item.size:
            job.success += 1
            job.downloaded_bytes += item.size
            job.log(f"✅ 完成：{item.rel_path}")
            # ⑥ 下载成功后删本地（可选的破坏性操作）
            await _maybe_delete_local(job, item)
            return
        if attempt + 1 < VERIFY_RETRIES:
            await asyncio.sleep(VERIFY_DELAY_SEC)

    reason = await _failure_reason(driver, task_id)
    job.record_failed(
        item, reason or "任务已结束但目标目录未出现同名同大小文件"
    )
    job.log(f"❌ 校验失败：{item.rel_path}")


async def _maybe_delete_local(job: OfflineJob, item: OfflineFile) -> None:
    """下载成功后删本地文件（**可选的破坏性操作**）。

    与「数据同步」的删除路径**同一套机制**
    （:mod:`app.services.upload_cleanup`），故三重保护一致：

    1. 云端确有同名同大小文件（**已在校验里确认过**）
    2. 路径安全：普通文件 / 非隐藏 / 在**源根目录**内
    3. 空目录一并删掉（向上递归，源根目录本身保留）

    **失败只记提示，不翻转结果**——下载已经成功了。
    """
    if not job.params.delete_after_upload:
        return

    # 源根目录 = 删除的边界（不能删到它外面去）
    root = Path(job.params.local_root)
    try:
        from app.services import upload_cleanup

        result = await asyncio.to_thread(
            upload_cleanup.delete_local_file, item.abs_path, root
        )
    except Exception as exc:
        job.log(f"⚠️ 删除本地文件失败：{item.rel_path}（{exc}）")
        return

    if result.status in (upload_cleanup.DELETE_OK, upload_cleanup.DELETE_MISSING):
        job.local_deleted += 1
        job.log(f"🗑️ 已删除本地文件：{item.rel_path}")
        for directory in result.removed_dirs:
            job.log(f"🗑️ 已删除空目录：{directory}")
    else:
        job.log(
            f"⚠️ 本地未删除：{item.rel_path}（{result.reason or result.status}）"
        )


async def _failure_reason(driver: Any, task_id: str) -> str:
    """读光鸭给出的失败原因（查不到返回空串）。"""
    try:
        items = await driver.list_failed_offline_tasks()
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


__all__ = [
    "OfflineDownloadError",
    "OfflineFile",
    "OfflineJob",
    "OfflineParams",
    "build_url",
    "collect_files",
    "run_offline_download",
]
