"""监听器管理器：把「监听 → 队列 → 上传」串成一条流水线。

**一个任务一条流水线**：

```
watchfiles.awatch(目录)
    → 稳态判定（upload_filter.is_stable）
    → asyncio.Queue（按路径去重）
    → N 个 worker
        → uploader.sync_file（比对 → 决策 → 传/删/跳过）
```

**为什么用队列而不是直接传**：上传是慢 IO（几 MB 到几百 MB），
多个文件同时变更时要排队，不能让监听协程卡在传输上。

**为什么按路径去重**：同一文件可能连续触发多个事件。队列满了会**丢弃
并计数**（而不是阻塞监听）——监听的职责是「不漏」，真漏了还有「立即
全量同步」兜底。

**生命周期**：进程内常驻协程（不是 APScheduler——本项目已移除调度模块）。
启动时按 DB 拉起所有启用的任务；任务增删改时动态增删监听器。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.adapters.drivers.base import DriverError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.models import UploadTask as UploadTaskRow
from app.db.session import session_scope
from app.services.cloud_index import CloudDirIndex
from app.services.upload_filter import aiter_local_files
from app.services.upload_paths import resolve_watch_dir
from app.services.upload_tasks import get_runtime
from app.services.uploader import (
    RESULT_CONFLICT,
    RESULT_FAILED,
    RESULT_OVERWRITTEN,
    RESULT_UNCHANGED,
    RESULT_UPLOADED,
    SyncContext,
    drain_cleanup_tasks,
    sync_file,
)
from app.services.watcher import Watcher

logger = get_logger(__name__)

#: 活跃的监听器：task_id → Watcher
_WATCHERS: dict[int, Watcher] = {}

#: 活跃的上传 worker：task_id → asyncio.Task
_WORKERS: dict[int, asyncio.Task[None]] = {}

#: 活跃的队列：task_id → asyncio.Queue
_QUEUES: dict[int, asyncio.Queue[Path]] = {}

#: 每个任务的上传上下文（``sync_now`` 要用同一个，否则缓存不共享）
_CONTEXTS: dict[int, SyncContext] = {}

#: 每个任务的**同步锁**：全量同步与事件流 worker 互斥（串行）。
#:
#: **为什么需要**：全量同步与事件流共享同一个 ``CloudDirIndex`` + 队列。
#: 并行跑虽靠幂等判据不会传重，但 ``runtime.current`` 会互相覆盖、
#: 日志交错难读、CIFS 删除集中爆发会拖慢响应。串行后：全量同步跑时
#: worker 等它结束再处理队列里积压的新事件——新文件不会被漏，只是延后。
_SYNC_LOCKS: dict[int, asyncio.Lock] = {}

#: 正在跑的全量同步任务：task_id → asyncio.Task
#:
#: **必须单独记**：``sync_now`` 是独立协程（不由 worker 执行），
#: 停监听器不会停它。停用任务时必须显式取消——否则「停用」后还在传。
_SYNC_TASKS: dict[int, asyncio.Task[None]] = {}


@dataclass
class _StartParams:
    """`start_all` 用的任务参数快照（避开 session 关闭后 ORM 对象过期）。"""

    task_id: int
    driver_id: int
    target_file_id: str
    watch_root: str
    include_subdirs: bool
    concurrency: int
    name: str
    delete_after_upload: bool


def get_watcher(task_id: int) -> Watcher | None:
    """取某个任务的监听器。"""
    return _WATCHERS.get(task_id)


def active_task_ids() -> list[int]:
    """正在监听的��务 id。"""
    return sorted(_WATCHERS.keys())


async def start_all() -> None:
    """启动所有启用的任务（应用启动时调用）。"""
    try:
        async with session_scope() as db:
            rows = (
                await db.execute(
                    select(UploadTaskRow).where(UploadTaskRow.enabled.is_(True))
                )
            ).scalars().all()
            # 转成普通结构，避开 session 关闭后属性过期
            tasks = [
                _StartParams(
                    task_id=row.id,
                    driver_id=row.driver_id,
                    target_file_id=row.target_file_id,
                    watch_root=row.watch_root,
                    include_subdirs=row.include_subdirs,
                    concurrency=row.concurrency,
                    name=row.name,
                    delete_after_upload=row.delete_after_upload,
                )
                for row in rows
            ]
    except Exception:
        logger.exception("加载上传任务失败")
        return

    for params in tasks:
        try:
            await start_watcher(
                task_id=params.task_id,
                driver_id=params.driver_id,
                target_file_id=params.target_file_id,
                watch_root=params.watch_root,
                include_subdirs=params.include_subdirs,
                concurrency=params.concurrency,
                name=params.name,
                delete_after_upload=params.delete_after_upload,
            )
        except Exception as exc:
            logger.exception("启动上传任务失败：%s（%s）", params.name, exc)


async def stop_all() -> None:
    """停止所有监听器（应用停机时调用）。"""
    for task_id in list(_WATCHERS.keys()):
        await stop_watcher(task_id)
    # 等后台删除全部完成（stop_watcher 里已等过，这里兜底——
    # 可能是在 stop_watcher 之外派生的后台任务）
    await drain_cleanup_tasks(wait_sec=30.0)
    logger.info("已停止全部上传监听器")


async def start_watcher(
    *,
    task_id: int,
    driver_id: int,
    target_file_id: str,
    watch_root: str,
    include_subdirs: bool,
    concurrency: int,
    name: str = "",
    delete_after_upload: bool = False,
) -> Watcher:
    """为一个任务启动监听器 + 上传 worker。

    Raises:
        DriverError: 存储源不可用 / 凭据缺失。
        ValueError: 监听目录非法。
    """
    await stop_watcher(task_id)

    settings = get_settings()
    watch_dir = resolve_watch_dir(watch_root)

    driver, uploader = await _build_clients(driver_id)

    queue: asyncio.Queue[Path] = asyncio.Queue(
        maxsize=settings.upload_queue_maxsize
    )
    _QUEUES[task_id] = queue
    # 同步锁：全量同步与事件流 worker 互斥（串行），见 _SYNC_LOCKS 注释
    _SYNC_LOCKS[task_id] = asyncio.Lock()

    watcher = Watcher(
        root=watch_dir,
        include_subdirs=include_subdirs,
        polling=settings.upload_watch_polling,
        poll_ms=settings.upload_watch_poll_ms,
        debounce_ms=settings.upload_watch_debounce_ms,
        stable_check_sec=settings.upload_stable_check_sec,
        dispatch=_make_dispatcher(task_id, queue),
    )
    # **必须登记进 ``_WATCHERS``**——``get_watcher`` / ``sync_now`` /
    # ``stop_all`` 都查它。只放进 ``runtime.watcher`` 的话，
    # 「立即全量同步」会误报「任务未在运行」。
    _WATCHERS[task_id] = watcher

    runtime = get_runtime(task_id)
    runtime.watcher = watcher
    runtime.queue = queue
    runtime.listening = True
    runtime.error = None
    runtime.log(f"开始监听：{watch_dir}（{name or task_id}）")

    await watcher.start()

    # 上传 worker
    ctx = SyncContext(
        driver=driver,
        uploader=uploader,
        index=CloudDirIndex(driver, target_file_id),
        root=watch_dir,
        delete_retries=settings.upload_delete_verify_retries,
        delete_delay=settings.upload_delete_verify_delay_sec,
        # 上传成功后清理本地文件（任务级开关，默认关）
        delete_local=delete_after_upload,
    )
    _CONTEXTS[task_id] = ctx
    worker = asyncio.create_task(_worker_loop(task_id, queue, ctx, concurrency))
    _WORKERS[task_id] = worker

    # **启动时先跑一次全量同步**——把监听目录里**已有的历史文件**同步上去。
    #
    # 为什么：容器重启期间、上次停机前的遗留文件、网络文件系统事件丢失，
    # 都会让「事件流」漏文件。全量比对能补齐——「同名+同大小」自动跳过，
    # 不会重复传。
    #
    # 异步触发，不阻塞启动：全量同步可能要几分钟（文件多），阻塞会让
    # lifespan 超时 / 健康检查失败。失败只记日志（与 ``sync_now`` 同口径）。
    #
    # 注册进 ``_SYNC_TASKS``——``stop_watcher`` 能取消它；用户在启动同步
    # 进行中点「立即全量同步」会收到「已有同步在跑」提示（合理，避免重复跑）。
    initial = asyncio.create_task(_initial_sync(task_id))
    _SYNC_TASKS[task_id] = initial

    return watcher


async def _initial_sync(task_id: int) -> None:
    """启动时的全量同步（异步触发，不阻塞启动）。

    与 :func:`sync_now` 同逻辑，但：
    - 不抛错（失败只记日志，启动不该因单个任务失败而红）
    - 不查「已有同步在跑」（启动时不会有，且查了反而误拦）
    - **不自己注册 ``_SYNC_TASKS``**——调用方（``start_watcher``）已注册
    """
    try:
        runtime = get_runtime(task_id)
        watcher = _WATCHERS.get(task_id)
        if watcher is None:
            return  # 已被停用
        await _sync_now_inner(task_id, runtime, watcher)
    except Exception:
        logger.exception("启动全量同步失败（task_id=%s）", task_id)
    finally:
        # 完成后从 _SYNC_TASKS 摘掉（只摘自己——用户可能后续手动触发新的）
        current = asyncio.current_task()
        if current is not None and _SYNC_TASKS.get(task_id) is current:
            _SYNC_TASKS.pop(task_id, None)


async def stop_watcher(task_id: int) -> None:
    """停止某个任务的监听器 + worker + **正在跑的全量同步**。

    **全量同步也必须停**：``sync_now`` 是独立协程（不由 worker 执行），
    停监听器**不会**停它——曾经「停用任务」后上传还在继续（用户以为停了，
    实际还在传 7GB）。故显式取消。
    """
    watcher = _WATCHERS.pop(task_id, None)
    if watcher is not None:
        await watcher.stop()
    worker = _WORKERS.pop(task_id, None)
    if worker is not None:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    # 取消正在跑的全量同步（停用任务时必须真的停）
    sync_task = _SYNC_TASKS.pop(task_id, None)
    if sync_task is not None:
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
    # 等后台删除完成（停机时保证「本地文件状态确定」——不留「以为删了
    # 其实还在」的中间态）。删除是异步的，可能还在跑。
    await drain_cleanup_tasks(wait_sec=20.0)
    _QUEUES.pop(task_id, None)
    _CONTEXTS.pop(task_id, None)
    _SYNC_LOCKS.pop(task_id, None)
    runtime = get_runtime(task_id)
    runtime.listening = False
    runtime.watcher = None
    runtime.queue = None
    runtime.syncing = False


def _make_dispatcher(task_id: int, queue: asyncio.Queue[Path]) -> Any:
    """构造投递回调（把稳态文件塞进队列）。"""

    async def _dispatch(path: Path) -> None:
        try:
            queue.put_nowait(path)
        except asyncio.QueueFull:
            runtime = get_runtime(task_id)
            runtime.log(f"⚠️ 上传队列已满，丢弃：{path.name}")

    return _dispatch


async def _build_clients(driver_id: int) -> tuple[Any, Any]:
    """构造目标网盘的驱动与上传器。

    **按驱动类型分派**（见 :mod:`app.services.upload_driver_factory`）——
    上层只认 :class:`app.adapters.drivers.upload_base.UploadDriver` 协议，
    不认具体网盘。新增支持上传的网盘只改工厂，前端零改动。
    """
    from app.services.upload_driver_factory import build_upload_clients

    async with session_scope() as db:
        row = (
            await db.execute(select(Driver).where(Driver.id == driver_id))
        ).scalar_one_or_none()
        if row is None:
            raise DriverError(f"存储源不存在：{driver_id}", kind="not_found")
        credential = row.credential
        device_type = row.device_type
        driver_type = row.type

    async def _on_refresh(cred: Any) -> None:
        # Token 刷新后写回 DB（光鸭的 token 只有 1~2 小时有效）。
        from app.services.migration import persist_guangya_credential

        await persist_guangya_credential(driver_id, cred)

    return await build_upload_clients(
        driver_type,
        credential=credential,
        device_type=device_type,
        on_token_refresh=_on_refresh,
    )


async def _worker_loop(
    task_id: int,
    queue: asyncio.Queue[Path],
    ctx: SyncContext,
    concurrency: int,
) -> None:
    """上传 worker 主循环（一个任务一个协程，内部串行处理）。

    **为什么内部串行**：瓶颈是**本机上行带宽**（~3.2 MB/s），
    并发换不来吞吐——单条 TCP 流已吃满上行，并发只是重新分配带宽，
    反而让服务端不稳（卡死 / ``Server disconnected``）。实测数据见
    ``docs/upload-bandwidth.md``。

    **故 ``concurrency`` 参数保留但不生效**（面板上仍可配，但恒为 1）——
    留着是为了将来换网盘（别的网盘支持并发时改这一处即可）。
    """
    while True:
        path = await queue.get()
        try:
            await _process_one(task_id, ctx, path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("上传处理异常：%s", path)
        finally:
            queue.task_done()


def _make_progress(runtime: Any, rel_path: str, total_size: int) -> Any:
    """构造上传进度回调（每 5% 记一行）。

    **为什么要节流**：大文件 5GB / 5MB 一片 = 1000 片，每片记一行
    会把环形日志（300 行）冲光，真正的关键信息（哪个文件失败）就没了。
    故每 5% 才记一行。
    """
    state = {"last": -1}

    def _on_progress(sent: int) -> None:
        if total_size <= 0:
            return
        percent = int(sent * 100 / total_size)
        if percent // 5 != state["last"] // 5:
            state["last"] = percent
            runtime.log(f"   ⏳ {rel_path} — {percent}%")

    return _on_progress


async def _process_one(task_id: int, ctx: SyncContext, path: Path) -> None:
    """处理一个文件（比对 → 决策 → 传）。"""
    from app.services.upload_filter import relative_to_root, should_skip

    runtime = get_runtime(task_id)

    reason = should_skip(path, root=ctx.root)
    if reason is not None:
        runtime.log(f"⏭️ 跳过 {path.name}（{reason}）")
        return

    rel_path = relative_to_root(path, ctx.root)
    runtime.current = rel_path
    try:
        file_size = path.stat().st_size
    except OSError:
        file_size = 0

    # **与全量同步互斥**：同一时刻只有一个协程调 sync_file。
    # 全量同步跑时 worker 在此等待——新事件不丢，只是延后处理。
    # ``lock`` 可能为 None（测试环境直接调本函数，未走 start_watcher）→
    # 退化为不互斥（测试本就是单协程串行）。
    lock = _SYNC_LOCKS.get(task_id)
    if lock is not None:
        async with lock:
            outcome = await sync_file(
                ctx,
                path,
                rel_path,
                on_progress=_make_progress(runtime, rel_path, file_size),
            )
    else:
        outcome = await sync_file(
            ctx,
            path,
            rel_path,
            on_progress=_make_progress(runtime, rel_path, file_size),
        )

    if outcome.status == RESULT_UPLOADED:
        runtime.total_uploaded += 1
        runtime.total_bytes += outcome.size
        note = "（本地删除中）" if outcome.local_deleted else ""
        if outcome.reason:
            note = f"（{outcome.reason}）"
        runtime.log(
            f"✅ 已上传：{rel_path}（{outcome.size / 1024**2:.1f} MB）{note}"
        )
    elif outcome.status == RESULT_OVERWRITTEN:
        runtime.total_uploaded += 1
        runtime.total_bytes += outcome.size
        note = "（本地删除中）" if outcome.local_deleted else ""
        if outcome.reason:
            note = f"（{outcome.reason}）"
        runtime.log(
            f"♻️ 已覆盖：{rel_path}（{outcome.size / 1024**2:.1f} MB）{note}"
        )
    elif outcome.status == RESULT_UNCHANGED:
        note = "（本地删除中）" if outcome.local_deleted else (
            f"（{outcome.reason}）" if outcome.reason else ""
        )
        runtime.log(f"⏭️ 已存在：{rel_path}{note}")
    elif outcome.status == RESULT_CONFLICT:
        runtime.total_failed += 1
        runtime.log(f"⚠️ 冲突：{rel_path}（{outcome.reason}）")
    elif outcome.status == RESULT_FAILED:
        runtime.total_failed += 1
        runtime.log(f"❌ 失败：{rel_path}（{outcome.reason}）")
    runtime.current = ""


async def sync_now(task_id: int) -> dict[str, Any]:
    """立即跑一次全量同步（手动补偿）。

    事件流会漏（容器重启期间、网络文件系统事件丢失），全量比对能补齐。

    **注册到 ``_SYNC_TASKS``** —— 停用任务时要能取消它（否则「停用」后
    上传还在继续，用户以为停了实际没停）。

    Returns:
        统计字典。

    Raises:
        DriverError: 任务不在运行 / 驱动不可用 / 已有同步在跑。
    """
    runtime = get_runtime(task_id)
    watcher = _WATCHERS.get(task_id)
    if watcher is None:
        raise DriverError("任务未在运行（监听器没起来）", kind="config")

    # 已经在跑 → 不重复启动
    existing = _SYNC_TASKS.get(task_id)
    if existing is not None and not existing.done():
        raise DriverError("该任务已有全量同步在跑", kind="conflict")

    current = asyncio.current_task()
    if current is not None:
        _SYNC_TASKS[task_id] = current
    try:
        return await _sync_now_inner(task_id, runtime, watcher)
    finally:
        if current is not None and _SYNC_TASKS.get(task_id) is current:
            _SYNC_TASKS.pop(task_id, None)


async def _sync_now_inner(task_id: int, runtime: Any, watcher: Any) -> dict[str, Any]:
    """全量同步的主体（见 :func:`sync_now`）。

    **边扫描边同步**：文件边扫边传，不等扫描全部结束——
    CIFS 上扫描可能要几小时，等扫完再传等于用户几小时看不到动静。
    """
    runtime.syncing = True
    runtime.log("开始全量同步…")
    try:
        ctx = _CONTEXTS.get(task_id)
        if ctx is None:
            raise DriverError("上传上下文不存在", kind="config")

        uploaded = overwritten = unchanged = failed = conflicts = 0
        total_bytes = 0
        deleted_local = 0
        # **与事件流 worker 互斥**：同一时刻只有一个协程调 sync_file。
        # 全量同步跑时 worker 在 _process_one 里等锁——新事件不丢，只是延后。
        # ``lock`` 可能为 None（测试环境直接调本函数，未走 start_watcher）→
        # 退化为不互斥（测试本就是单协程串行）。
        lock = _SYNC_LOCKS.get(task_id)
        index = 0
        async for path in aiter_local_files(
            watcher.root, include_subdirs=watcher.include_subdirs
        ):
            index += 1
            rel = watcher.relative(path)
            runtime.current = rel
            # **每个文件都记一行** —— 大文件上传要几分钟，没有进度
            # 用户会以为卡死了。格式与事件驱动的路径一致。
            runtime.log(f"⏳ [{index}] 处理中：{rel}")
            try:
                file_size = path.stat().st_size
            except OSError:
                file_size = 0
            if lock is not None:
                async with lock:
                    outcome = await sync_file(
                        ctx,
                        path,
                        rel,
                        on_progress=_make_progress(runtime, rel, file_size),
                    )
            else:
                outcome = await sync_file(
                    ctx,
                    path,
                    rel,
                    on_progress=_make_progress(runtime, rel, file_size),
                )
            if outcome.local_deleted:
                deleted_local += 1
            if outcome.status == RESULT_UPLOADED:
                uploaded += 1
                total_bytes += outcome.size
                # **同步累加到运行态**——否则面板的「上传 N」在跑全量
                # 同步时恒为 0（只累加局部变量，运行态不动）。
                # 用 ``+=``：``runtime`` 是跨多次同步的累计值。
                runtime.total_uploaded += 1
                runtime.total_bytes += outcome.size
                runtime.log(
                    f"✅ [{index}] 已上传：{rel}"
                    f"（{outcome.size / 1024**2:.1f} MB）"
                    + ("（本地删除中）" if outcome.local_deleted else "")
                )
            elif outcome.status == RESULT_OVERWRITTEN:
                overwritten += 1
                total_bytes += outcome.size
                runtime.total_uploaded += 1
                runtime.total_bytes += outcome.size
                runtime.log(
                    f"♻️ [{index}] 已覆盖：{rel}"
                    f"（{outcome.size / 1024**2:.1f} MB）"
                    + ("（本地删除中）" if outcome.local_deleted else "")
                )
            elif outcome.status == RESULT_UNCHANGED:
                unchanged += 1
                if outcome.local_deleted:
                    deleted_local += 1
                note = "（本地删除中）" if outcome.local_deleted else (
                    f"（{outcome.reason}）" if outcome.reason else ""
                )
                runtime.log(f"⏭️ [{index}] 已存在：{rel}{note}")
            elif outcome.status == RESULT_CONFLICT:
                conflicts += 1
                runtime.total_failed += 1
                runtime.log(f"⚠️ [{index}] 冲突：{rel}（{outcome.reason}）")
            elif outcome.status == RESULT_FAILED:
                failed += 1
                runtime.total_failed += 1
                runtime.log(f"❌ [{index}] 失败：{rel}（{outcome.reason}）")
        runtime.current = ""

        runtime.log(
            f"全量同步完成：共 {index} 个文件，上传 {uploaded}，覆盖 {overwritten}，"
            f"已存在 {unchanged}，失败 {failed}，冲突 {conflicts}"
            + (f"，本地已安排删除 {deleted_local}" if deleted_local else "")
        )
        return {
            "uploaded": uploaded,
            "overwritten": overwritten,
            "unchanged": unchanged,
            "failed": failed,
            "conflicts": conflicts,
            "deleted_local": deleted_local,
            "uploaded_bytes": total_bytes,
        }
    finally:
        runtime.syncing = False


__all__ = [
    "active_task_ids",
    "get_watcher",
    "start_all",
    "start_watcher",
    "stop_all",
    "stop_watcher",
    "sync_now",
]
