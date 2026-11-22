"""监听器管理器：把「监听 → 队列 → 上传」串成一条流水线。

**一个任务两条通道**（2026-11-07 改造，原「一条流水线」）::

    watchfiles.awatch(目录)
        → 稳态判定（upload_filter）
        → 按文件大小分流（``upload_small_file_threshold``，默认 1MB）
            ├─ 大文件队列 → 1 个 worker（**恒串行**）
            └─ 小文件队列 → K 个 worker（K = 任务 ``concurrency`` 列）

**为什么大文件恒串行**：瓶颈是**本机上行带宽**（~3.2 MB/s），
并发换不来吞吐——单条 TCP 流已吃满上行，并发只是重新分配带宽，
反而让服务端不稳（卡死 / ``Server disconnected``）。实测数据见
``docs/upload-bandwidth.md``。这是**正确实现，不是待优化的遗留**。

**为什么小文件可以并发**：小文件（<1MB 单 PUT，不分片）的耗时大头是
**协议开销**——取上传凭证 + ``upload_info`` 轮询落盘确认，每个文件
~2-3 秒，传字节占比 <15%。串行传 1000 个元数据文件要 25-50 分钟，
带宽全程空闲；并发把轮询等待摊掉（收益 2-3 倍，受全局限速 5 req/s
约束）。``<1MB 单 PUT`` 是实测**唯一支持并发**的上传路径。并发受
:class:`_SmallGate` 动态调控：大文件上传中 / 服务端熔断时自动降为 1。

**为什么用队列而不是直接传**：上传是慢 IO（几 MB 到几百 MB），
多个文件同时变更时要排队，不能让监听协程卡在传输上。

**为什么全局按路径去重**（:data:`_INFLIGHT`）：同一文件可能连续触发
多个事件；更重要的是**同一文件绝不能被两个协程同时上传**——两边都
比对出「云端没有」→ 都传 → 光鸭自动改名 ``xxx(1).bin``（实测行为）
→ 云端垃圾副本灾难。事件流两条通道之间、事件流与全量同步之间，全靠
这个集合互斥（原 ``_SYNC_LOCKS`` 只护大文件通道与全量同步，分流后
小通道不持锁，必须另有闸门）。

**队列满了怎么办**：不丢——进「溢出暂存」（:data:`_OVERFLOW`，按
路径去重），worker 每处理完一个文件就把暂存回灌队列；只有暂存也满
（:data:`_OVERFLOW_LIMIT`）才真丢并计数。「立即全量同步」仍是最后
兜底（手动）。

**生命周期**：进程内常驻协程（不是 APScheduler——本项目已移除调度模块）。
启动时按 DB 拉起所有启用的任务；任务增删改时动态增删监听器。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.adapters.drivers.base import DriverError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.models import UploadTask as UploadTaskRow
from app.db.session import session_scope
from app.services import aggregate_staging, aggregate_upload
from app.services.aggregate_upload import LandingCandidate
from app.services.cloud_index import CloudDirIndex
from app.services.upload_filter import aiter_local_files, relative_to_root, should_skip
from app.services.upload_paths import resolve_watch_dir
from app.services.upload_tasks import get_runtime
from app.services.uploader import (
    CONFLICT_KEEP_LARGER,
    RESULT_CONFLICT,
    RESULT_FAILED,
    RESULT_OVERWRITTEN,
    RESULT_UNCHANGED,
    RESULT_UPLOADED,
    StagedHit,
    SyncContext,
    SyncOutcome,
    base_name,
    drain_cleanup_tasks,
    parent_dir,
    sync_file,
)
from app.services.watcher import Watcher

logger = get_logger(__name__)

#: 监听目录检查超时（秒）——挂载无响应时的兜底，避免 lifespan 卡死
WATCH_DIR_RESOLVE_TIMEOUT_SEC = 15.0

#: 小通道熔断：连续失败 N 次 → 降为串行 + 冷却
#:
#: ``Server disconnected`` / 卡死是实测的并发症状——连续失败大概率是
#: 服务端扛不住，立即止损。
_BREAKER_FAIL_STREAK = 3

#: 熔断冷却（秒）
_BREAKER_COOLDOWN_SEC = 60.0

#: 熔断恢复：降级期间**连续成功** N 次才重新放开并发
#: （防止「刚恢复就再次打挂服务端」的振荡）
_BREAKER_RECOVER_SUCCESS = 10

#: 溢出暂存上限（条）——超过才真丢（并计数）。暂存是内存 dict，
#: 上限防的是「无限事件风暴」下的内存增长。
_OVERFLOW_LIMIT = 5000

#: 队列条目：路径 + 入队时的 size。
#:
#: size 是**分流依据**（稳态采样时已拿到，不二次 stat），也用于进度
#: 显示；权威大小由 :func:`sync_file` 现场比对（同名+同大小=已同步）。
@dataclass(slots=True)
class _Item:
    """一条待上传的稳态文件。"""

    path: Path
    size: int


class _SmallGate:
    """小文件通道的**动态并发闸**。

    K 个 worker 常驻（K = 任务 ``concurrency`` 列），同一时刻最多
    ``effective_limit()`` 个 worker 在传：

    - **大文件上传中 → 1**：小文件的字节占比极小，但并发请求会与
      大文件抢限速配额与带宽——保守起见让路（大文件传完立即恢复 K）。
    - **熔断 → 1 + 冷却**：连续失败 3 次 → 冷却 60s（期间串行）；
      冷却结束后**仍是串行**，连续成功 10 次才重新放开并发——
      避免「刚恢复就再次打挂服务端」的振荡。

    实现用 ``asyncio.Condition`` + 定时重查：冷却到期**没有人 notify**
    （没有定时器），靠 0.5s 的 ``wait_for`` 超时兜底唤醒重查。
    """

    def __init__(self, k: int) -> None:
        self._cond = asyncio.Condition()
        #: 静态并发上限（任务 concurrency 列）
        self.k = max(1, int(k))
        #: 大文件正在上传（大文件 worker 置位 / 复位）
        self.big_active = False
        #: 熔断降级中（连续成功达标后解除）
        self.derated = False
        #: 熔断冷却截止时刻（monotonic 秒）
        self.resume_at = 0.0
        #: 连续失败计数（成功清零）
        self.fail_streak = 0
        #: 降级期间的连续成功计数
        self.success_streak = 0
        #: 当前持闸的 worker 序号
        self._active: set[int] = set()

    def effective_limit(self) -> int:
        """当前允许的并发数。"""
        if time.monotonic() < self.resume_at:
            return 1  # 冷却中
        if self.derated:
            return 1  # 熔断恢复期，先串行观察
        if self.big_active:
            return 1  # 大文件上传中，让路
        return self.k

    async def acquire(self, ordinal: int) -> None:
        """占一个并发名额（满了就等：被 notify 或 0.5s 超时后重查）。"""
        async with self._cond:
            while len(self._active) >= self.effective_limit():
                # 冷却到期 / 限值变化没人 notify——0.5s 超时兜底唤醒重查
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._cond.wait(), timeout=0.5)
            self._active.add(ordinal)

    async def release(self, ordinal: int) -> None:
        """归还并发名额并唤醒等待者。"""
        async with self._cond:
            self._active.discard(ordinal)
            self._cond.notify_all()

    async def set_big_active(self, active: bool) -> None:
        """大文件上传开始 / 结束（大文件 worker 调）。"""
        async with self._cond:
            self.big_active = active
            self._cond.notify_all()

    def record_failure(self) -> bool:
        """记录一次失败。返回是否**触发熔断**。"""
        self.fail_streak += 1
        if self.fail_streak < _BREAKER_FAIL_STREAK:
            return False
        self.fail_streak = 0
        self.derated = True
        self.success_streak = 0
        self.resume_at = time.monotonic() + _BREAKER_COOLDOWN_SEC
        return True

    def record_success(self) -> None:
        """记录一次成功（失败计数清零；降级期间攒够连续成功就恢复并发）。"""
        self.fail_streak = 0
        if self.derated:
            self.success_streak += 1
            if self.success_streak >= _BREAKER_RECOVER_SUCCESS:
                self.derated = False
                self.success_streak = 0


#: 活跃的监听器：task_id → Watcher
_WATCHERS: dict[int, Watcher] = {}

#: 活跃的上传 worker：task_id → [大文件 worker ×1, 小文件 worker ×K]
_WORKERS: dict[int, list[asyncio.Task[None]]] = {}

#: 大文件队列：task_id → asyncio.Queue
_QUEUES_LARGE: dict[int, asyncio.Queue[_Item]] = {}

#: 小文件队列：task_id → asyncio.Queue
_QUEUES_SMALL: dict[int, asyncio.Queue[_Item]] = {}

#: 每个任务的上传上下文（``sync_now`` 要用同一个，否则缓存不共享）
_CONTEXTS: dict[int, SyncContext] = {}

#: 全局「处理中」集合：task_id → {相对路径}
#:
#: **同一文件同一时刻只允许一个协程处理**——dispatch 入队成功时登记、
#: worker 处理完移除、全量同步扫到「在队」的文件时跳过。
_INFLIGHT: dict[int, set[str]] = {}

#: 溢出暂存：task_id → {相对路径: (路径, 大小)}——队列满时先进这里
_OVERFLOW: dict[int, dict[str, tuple[Path, int]]] = {}

#: 小通道动态并发闸：task_id → _SmallGate
_SMALL_GATES: dict[int, _SmallGate] = {}

#: 每个任务的**同步锁**：全量同步与大文件通道互斥（串行）。
#:
#: **为什么需要**：全量同步与大文件通道共享同一个 ``CloudDirIndex``。
#: 并行跑虽靠幂等判据不会传重，但 ``runtime.current`` 会互相覆盖、
#: 日志交错难读、CIFS 删除集中爆发会拖慢响应。串行后：全量同步跑时
#: 大文件 worker 等它结束再处理队列里积压的新事件——新文件不会被漏，
#: 只是延后。
#:
#: **小文件通道不持锁**——小通道与全量同步/大文件通道的「同文件双传」
#: 防护由 :data:`_INFLIGHT` 承担（更精确：按路径而非按通道互斥），
#: 不互斥才能在大文件上传的几十分钟里持续泄洪小文件。
_SYNC_LOCKS: dict[int, asyncio.Lock] = {}

#: 正在跑的全量同步任务：task_id → asyncio.Task
#:
#: **必须单独记**：``sync_now`` 是独立协程（不由 worker 执行），
#: 停监听器不会停它。停用任务时必须显式取消——否则「停用」后还在传。
_SYNC_TASKS: dict[int, asyncio.Task[None]] = {}

#: 聚合任务的**落点状态**：task_id → _Landing
#:
#: 非空 = 这个任务的目标是**聚合组**（落点由分配器在组内副盘之间挑选）。
_LANDINGS: dict[int, _Landing] = {}

#: 候选缓存的有效期（秒）——避免每个文件都查一次 DB
_CANDIDATES_TTL_SEC = 60.0


@dataclass(slots=True)
class _Landing:
    """聚合任务的落点状态（**进程内**）。

    持久化的那份在 ``upload_tasks.driver_id`` / ``target_file_id``
    （粘性——重启后恢复，避免已传文件被重传）。

    **组级幂等索引挂在落点上**（:attr:`staged_index`）——它本来就是
    「这个任务往组里放过什么」的记录，与落点同生共死（任务停 → 落点
    与索引一起清掉），不另设一份全局表。
    """

    group_id: int
    driver_id: int
    driver_name: str
    root_id: str
    root_path: str | None
    delete_after_upload: bool
    conflict_policy: int
    #: 任务级的「目标子目录」（组映射的子路径覆盖）——**空 = 无覆盖**。
    #:
    #: 落在副盘缓冲根下的同名子目录；归拢引擎「相对结构原样保留」→
    #: 主盘侧自动落到「归拢目标目录」的同名子目录下（归拢零改动）。
    sub_path: str = ""
    #: 候选缓存（按 driver id 升序的固定顺序）
    candidates: list[LandingCandidate] = field(default_factory=list)
    #: 候选缓存的取样时刻（monotonic）
    candidates_at: float = 0.0
    #: **组级幂等索引**：``(相对目录, 文件名) → 台账条目``
    #:
    #: 落点会换盘，「当前落点目录里没有这个文件」≠「文件没传过」——
    #: 文件可能在**另一个副盘**的缓冲里，或**已经归拢到主盘**。
    staged_index: dict[tuple[str, str], aggregate_staging.StagedEntry] = field(
        default_factory=dict
    )

    def current_candidate(self) -> LandingCandidate | None:
        """当前落点在候选表里的那一条（None = 缓存里没有）。"""
        for cand in self.candidates:
            if cand.driver_id == self.driver_id:
                return cand
        return None


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
    conflict_policy: int
    #: 目标聚合组（非空 = 「本地同步到网盘聚合」）
    aggregate_group_id: int | None = None
    #: 目标子目录（聚合模式专属）：组映射的子路径覆盖，空 = 无覆盖
    aggregate_sub_path: str = ""


def get_watcher(task_id: int) -> Watcher | None:
    """取某个任务的监听器。"""
    return _WATCHERS.get(task_id)


def active_task_ids() -> list[int]:
    """正在监听的任务 id。"""
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
                    conflict_policy=row.conflict_policy,
                    aggregate_group_id=row.aggregate_group_id,
                    aggregate_sub_path=row.aggregate_sub_path or "",
                )
                for row in rows
            ]
    except Exception as exc:
        logger.exception("加载上传任务失败")
        from app.services.startup_status import record

        record("watch_manager_load", exc)
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
                conflict_policy=params.conflict_policy,
                aggregate_group_id=params.aggregate_group_id,
                aggregate_sub_path=params.aggregate_sub_path,
            )
        except Exception as exc:
            logger.exception("启动上传任务失败：%s（%s）", params.name, exc)
            from app.services.startup_status import record

            record(f"watch_manager_start:{params.name}", exc)


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
    conflict_policy: int = CONFLICT_KEEP_LARGER,
    aggregate_group_id: int | None = None,
    aggregate_sub_path: str = "",
) -> Watcher:
    """为一个任务启动监听器 + 上传 worker（大文件 ×1 + 小文件 ×K）。

    ``aggregate_group_id`` 非空 = **「本地同步到网盘聚合」**：落点不在
    ``driver_id`` 上，而是由分配器在组内副盘之间挑选（撞到月度上传限额
    就换下一个），上传成功后**即时登记台账**，归拢引擎接管搬运。

    ``aggregate_sub_path`` = 任务级的「目标子目录」（组映射的子路径覆盖，
    仅聚合模式有效）：文件落在副盘缓冲根下的该子目录里，归拢后落到主盘
    「归拢目标目录」的同名子目录下。

    Raises:
        DriverError: 存储源不可用 / 凭据缺失 / 聚合组不可用。
        ValueError: 监听目录非法。
    """
    await stop_watcher(task_id)

    settings = get_settings()
    threshold = settings.upload_small_file_threshold
    # ⚠️ **放线程池 + 加超时**：
    # 1) ``resolve_watch_dir`` 会 ``resolve()`` + ``is_dir()``，都是同步
    #    stat——监听根在 CIFS 上时，挂载卡死会让它**无限等待**；
    # 2) 在事件循环里跑会把启动路径（乃至整个面板）卡死；
    # 3) **必须加超时**——否则挂载坏着启动时 lifespan 永远走不完，
    #    uvicorn 压根不开始服务（2026-11-06 prod 事故：面板假死 8 分钟）。
    #    超时 = 这个监听器先不启动，记日志；挂载恢复后由探活/手动重试补上。
    try:
        watch_dir = await asyncio.wait_for(
            asyncio.to_thread(resolve_watch_dir, watch_root),
            timeout=WATCH_DIR_RESOLVE_TIMEOUT_SEC,
        )
    except TimeoutError as exc:
        raise DriverError(
            f"监听目录检查超时（{WATCH_DIR_RESOLVE_TIMEOUT_SEC}s）：{watch_root}"
            "——挂载可能无响应（NAS 离线 / CIFS 卡死）"
        ) from exc

    landing: _Landing | None = None
    if aggregate_group_id is not None:
        landing = await _resolve_landing(
            aggregate_group_id,
            preferred_driver_id=driver_id,
            delete_after_upload=delete_after_upload,
            conflict_policy=conflict_policy,
            sub_path=aggregate_sub_path,
        )
        driver, uploader = await _build_clients(landing.driver_id)
    else:
        driver, uploader = await _build_clients(driver_id)

    queue_large: asyncio.Queue[_Item] = asyncio.Queue(
        maxsize=settings.upload_queue_maxsize
    )
    queue_small: asyncio.Queue[_Item] = asyncio.Queue(
        maxsize=settings.upload_queue_maxsize
    )
    _QUEUES_LARGE[task_id] = queue_large
    _QUEUES_SMALL[task_id] = queue_small
    _INFLIGHT[task_id] = set()
    _OVERFLOW[task_id] = {}
    # 小通道动态并发闸：大文件上传中 / 熔断时自动降为 1
    gate = _SmallGate(concurrency)
    _SMALL_GATES[task_id] = gate
    # 同步锁：全量同步与大文件通道互斥（串行），见 _SYNC_LOCKS 注释
    _SYNC_LOCKS[task_id] = asyncio.Lock()

    watcher = Watcher(
        root=watch_dir,
        include_subdirs=include_subdirs,
        polling=settings.upload_watch_polling,
        poll_ms=settings.upload_watch_poll_ms,
        debounce_ms=settings.upload_watch_debounce_ms,
        stable_check_sec=settings.upload_stable_check_sec,
        dispatch=_make_dispatcher(
            task_id, watch_dir, queue_large, queue_small, threshold
        ),
    )
    # **必须登记进 ``_WATCHERS``**——``get_watcher`` / ``sync_now`` /
    # ``stop_all`` 都查它。只放进 ``runtime.watcher`` 的话，
    # 「立即全量同步」会误报「任务未在运行」。
    _WATCHERS[task_id] = watcher

    runtime = get_runtime(task_id)
    runtime.watcher = watcher
    runtime.queue_large = queue_large
    runtime.queue_small = queue_small
    runtime.listening = True
    runtime.error = None
    runtime.log(
        f"开始监听：{watch_dir}（{name or task_id}）；"
        f"分流阈值 {threshold} 字节，小通道并发 {max(1, concurrency)}"
    )

    await watcher.start()

    # 上传上下文
    if landing is not None:
        # 聚合模式：落点 = 分配器挑中的副盘缓冲根
        # **先登记落点**（幂等索引挂在落点上，探测回调要读它）
        _LANDINGS[task_id] = landing
        # 组级幂等索引（台账）——载入一次，之后登记时增量维护
        landing.staged_index = await aggregate_staging.load_staged_index(
            landing.group_id
        )
        ctx = SyncContext(
            driver=driver,
            uploader=uploader,
            index=CloudDirIndex(driver, landing.root_id),
            root=watch_dir,
            delete_retries=settings.upload_delete_verify_retries,
            delete_delay=settings.upload_delete_verify_delay_sec,
            delete_local=landing.delete_after_upload,
            conflict_policy=landing.conflict_policy,
            # 组级幂等探测：文件可能已在别的副盘缓冲里 / 已归拢到主盘
            staged_lookup=_make_staged_lookup(landing),
            # 任务级「目标子目录」→ 云端相对路径整体下移一层
            dir_prefix=landing.sub_path,
        )
        # 落点快照写回 DB（粘性——重启后恢复，避免已传文件被重传）
        await _persist_landing(task_id, landing)
        runtime.log(
            f"🎯 聚合模式：组 {landing.group_id} → 副盘 {landing.driver_name}"
            f"（{landing.root_path or landing.root_id}）"
            + (f" / 子目录「{landing.sub_path}」" if landing.sub_path else "")
        )
    else:
        ctx = SyncContext(
            driver=driver,
            uploader=uploader,
            index=CloudDirIndex(driver, target_file_id),
            root=watch_dir,
            delete_retries=settings.upload_delete_verify_retries,
            delete_delay=settings.upload_delete_verify_delay_sec,
            # 上传成功后清理本地文件（任务级开关，默认关）
            delete_local=delete_after_upload,
            # 同名且大小不同的冲突策略（任务级，默认「保留大的」）
            conflict_policy=conflict_policy,
        )
    _CONTEXTS[task_id] = ctx

    # 大文件通道：**恒 1 个 worker**——光鸭上传只能串行（实测见
    # ``docs/upload-bandwidth.md``），不是待优化的遗留。
    workers: list[asyncio.Task[None]] = [
        asyncio.create_task(_worker_loop(task_id, queue_large, ctx, gate=gate))
    ]
    # 小文件通道：K 个 worker（K = 任务 ``concurrency`` 列）。
    # K=1 时与大文件同款串行行为；并发受 _SmallGate 动态调控。
    # ``concurrency`` 列的语义就此落地——面板上的「同时进行的上传数」
    # 实际控制的是小文件通道。
    for ordinal in range(max(1, concurrency)):
        workers.append(
            asyncio.create_task(
                _small_worker_loop(
                    task_id, queue_small, ctx, ordinal=ordinal, gate=gate
                )
            )
        )
    _WORKERS[task_id] = workers
    runtime.workers = workers

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
    """停止某个任务的监听器 + 全部 worker + **正在跑的全量同步**。

    **全量同步也必须停**：``sync_now`` 是独立协程（不由 worker 执行），
    停监听器**不会**停它——曾经「停用任务」后上传还在继续（用户以为停了，
    实际还在传 7GB）。故显式取消。
    """
    watcher = _WATCHERS.pop(task_id, None)
    if watcher is not None:
        await watcher.stop()
    workers = _WORKERS.pop(task_id, None)
    for worker in workers or []:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    # 取消正在跑的全量同步（停用任务时必须真的停）
    sync_task = _SYNC_TASKS.pop(task_id, None)
    if sync_task is not None:
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
    # 等后台删除完成（停机时保证「本地文件状态确定」——不留「以为删了
    # 其实还在」的中间态）。删除是异步的，可能还在跑。
    await drain_cleanup_tasks(wait_sec=20.0)
    _QUEUES_LARGE.pop(task_id, None)
    _QUEUES_SMALL.pop(task_id, None)
    _CONTEXTS.pop(task_id, None)
    # 落点与它的组级幂等索引**一起清**（索引挂在落点上）
    _LANDINGS.pop(task_id, None)
    _SYNC_LOCKS.pop(task_id, None)
    _INFLIGHT.pop(task_id, None)
    _OVERFLOW.pop(task_id, None)
    _SMALL_GATES.pop(task_id, None)
    runtime = get_runtime(task_id)
    runtime.listening = False
    runtime.watcher = None
    runtime.queue_large = None
    runtime.queue_small = None
    runtime.current = ""
    runtime.current_small = ""
    runtime.overflow = 0
    runtime.syncing = False


def _make_dispatcher(
    task_id: int,
    root: Path,
    large_queue: asyncio.Queue[_Item],
    small_queue: asyncio.Queue[_Item],
    threshold: int,
) -> Any:
    """构造投递回调：把稳态文件**按大小分流**进两条队列。

    **去重与登记的顺序不能反**：查 ``_INFLIGHT`` → 入队 → 登记。
    检查、``put_nowait``、登记之间**没有 await**（全是同步调用）——
    asyncio 单线程下是原子的，不会出现「两个协程同时通过检查」。

    **队列满不丢**：进溢出暂存（:func:`_stash_overflow`），等 worker
    空闲回灌（:func:`_drain_overflow`）。
    """

    async def _dispatch(path: Path, size: int) -> None:
        rel = relative_to_root(path, root)
        inflight = _INFLIGHT.setdefault(task_id, set())
        if rel in inflight:
            # 同一路径已在队列 / 处理中（重复事件）——静默去重。
            # 这就是模块文档说的「按路径去重」：过去只在稳态判定层
            # 有效，队列层面同一文件可能占多个坑。
            return
        queue = small_queue if size < threshold else large_queue
        try:
            queue.put_nowait(_Item(path=path, size=size))
        except asyncio.QueueFull:
            _stash_overflow(task_id, rel, path, size)
            return
        inflight.add(rel)

    return _dispatch


def _stash_overflow(task_id: int, rel: str, path: Path, size: int) -> None:
    """队列满 → 溢出暂存（按路径去重，不阻塞监听）。

    同一路径已在暂存里就不重复记日志——事件风暴时**一行日志刷一遍**
    会把 300 行环形日志冲光（这正是 2026-11-07 生产「一直报错」的观感
    来源）。
    """
    runtime = get_runtime(task_id)
    box = _OVERFLOW.setdefault(task_id, {})
    if rel in box:
        return
    if len(box) >= _OVERFLOW_LIMIT:
        runtime.dropped += 1
        runtime.log(
            f"⚠️ 溢出暂存已满（{_OVERFLOW_LIMIT}），丢弃：{path.name}"
            f"（累计丢 {runtime.dropped}；可稍后「立即全量同步」补齐）"
        )
        return
    box[rel] = (path, size)
    runtime.overflow = len(box)
    runtime.log(f"⏸ 队列已满，溢出暂存：{rel}（待空闲自动回灌，暂存 {len(box)}）")


def _drain_overflow(task_id: int) -> None:
    """把溢出暂存回灌队列（worker 每处理完一个文件调一次）。

    **纯同步**（无 await）——在 worker 的 finally 里调用，不打断消费
    节奏。队列仍满就留着下次（``break``）；路径已在处理中（如全量同步
    接手）的暂存份直接作废——它会被活着的协程传上去，暂存份是重复的。
    """
    box = _OVERFLOW.get(task_id)
    if not box:
        return
    threshold = get_settings().upload_small_file_threshold
    large_queue = _QUEUES_LARGE.get(task_id)
    small_queue = _QUEUES_SMALL.get(task_id)
    inflight = _INFLIGHT.setdefault(task_id, set())
    for rel in list(box):
        if rel in inflight:
            del box[rel]
            continue
        path, size = box[rel]
        queue = small_queue if size < threshold else large_queue
        if queue is None:
            continue  # 通道已随任务停止——留给 stop_watcher 清理
        try:
            queue.put_nowait(_Item(path=path, size=size))
        except asyncio.QueueFull:
            break  # 还满——下次再试
        del box[rel]
        inflight.add(rel)
    runtime = get_runtime(task_id)
    runtime.overflow = len(box)


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
        driver_type = row.type

    from app.services.driver_service import guangya_refresh_callback

    return await build_upload_clients(
        driver_type,
        credential=credential,
        on_token_refresh=guangya_refresh_callback(driver_id),
    )


# --------------------------------------------------------------------------
# 聚合模式：落点分配 / 撞墙换盘 / 台账登记
# --------------------------------------------------------------------------


async def _resolve_landing(
    group_id: int,
    *,
    preferred_driver_id: int,
    delete_after_upload: bool,
    conflict_policy: int,
    sub_path: str = "",
) -> _Landing:
    """解析聚合任务的落点。

    **粘性**：优先沿用当前落点（``preferred_driver_id``——即任务行里存的
    快照），它失效了（移出组 / 停用 / 本月额度耗尽）才重新挑。

    ``sub_path`` = 任务级的「目标子目录」（组映射的子路径覆盖）——落点
    换盘时**跟着任务走**（新副盘的缓冲根下建同名子目录）。

    Raises:
        DriverError: 组不可用（不存在 / 停用 / 无主盘 / 未配归拢目标 /
            没有可用副盘）。
    """
    from app.services.errors import ServiceError

    try:
        await aggregate_upload.validate_group_target(group_id)
    except ServiceError as exc:
        raise DriverError(str(exc), kind="config") from exc

    candidates = await aggregate_upload.list_candidates(group_id)
    chosen: LandingCandidate | None = None
    for cand in candidates:
        if (
            cand.driver_id == preferred_driver_id
            and aggregate_upload.candidate_ok(cand, 0)
        ):
            chosen = cand
            break
    if chosen is None:
        chosen = await aggregate_upload.pick_candidate(group_id, 0)
    if chosen is None:
        raise DriverError(
            "聚合组内没有可用的副盘落点（副盘需启用、已配缓冲根、"
            "且本月上传额度未耗尽）",
            kind="config",
        )
    return _Landing(
        group_id=group_id,
        driver_id=chosen.driver_id,
        driver_name=chosen.driver_name,
        root_id=chosen.root_id,
        root_path=chosen.root_path,
        delete_after_upload=delete_after_upload,
        conflict_policy=conflict_policy,
        sub_path=sub_path,
        candidates=candidates,
        candidates_at=time.monotonic(),
    )


def _landing_display_path(landing: _Landing) -> str:
    """落点的展示路径（**只读**——副盘缓冲根 + 任务级子目录）。

    ⚠️ 这是**副盘侧**的路径（文件第一跳落点），不是主盘的最终落点——
    最终落点是主盘「归拢目标目录」下的同名子目录（归拢引擎负责搬运）。
    """
    base = landing.root_path or landing.root_id
    if landing.sub_path:
        return f"{base}/{landing.sub_path}"
    return base


async def _persist_landing(task_id: int, landing: _Landing) -> None:
    """把落点快照写回任务行（**粘性**——重启后恢复）。

    **只降级数据库故障**（``SQLAlchemyError``）：写库失败 → 记日志 + 登记
    降级台账（``/readyz`` 的 ``degradations`` 会显示「粘性落点没写成」），
    内存里的落点仍然正确，重启后重新解析即可。

    ⚠️ **其余异常一律冒泡**——``NameError`` / ``AttributeError`` 之类是
    **程序缺陷**，宽泛捕获会把它们降级成 warning，掩盖真 bug（启动路径
    由 ``start_all`` 兜住并登记台账）。
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.services.startup_status import clear, record

    component = f"upload_landing_persist:{task_id}"
    try:
        async with session_scope() as db:
            row = (
                await db.execute(
                    select(UploadTaskRow).where(UploadTaskRow.id == task_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.driver_id = landing.driver_id
            row.target_file_id = landing.root_id
            row.target_path = _landing_display_path(landing)
            await db.commit()
    except SQLAlchemyError as exc:
        logger.warning("落点快照写回失败（task_id=%s）：%s", task_id, exc)
        record(component, exc)
        return
    clear(component)


def _make_staged_lookup(landing: _Landing) -> Any:
    """构造**组级幂等探测**回调（挂到 :attr:`SyncContext.staged_lookup`）。

    落点会换盘，「当前落点目录里没有这个文件」≠「文件没传过」——
    文件可能在**另一个副盘**的缓冲里，或**已经归拢到主盘**。台账是判据。
    """

    async def _lookup(
        rel_dir: str, file_name: str, size: int
    ) -> StagedHit | None:
        entry = landing.staged_index.get((rel_dir, file_name))
        if entry is None or entry.size_bytes != size:
            return None
        return StagedHit(
            size=entry.size_bytes,
            file_id=entry.file_id,
            note=entry.note,
        )

    return _lookup


async def _refresh_candidates(
    landing: _Landing, *, force: bool = False
) -> list[LandingCandidate]:
    """取候选列表（带 TTL 缓存——避免每个文件都查一次 DB）。"""
    now = time.monotonic()
    if (
        not force
        and landing.candidates
        and (now - landing.candidates_at) < _CANDIDATES_TTL_SEC
    ):
        return landing.candidates
    landing.candidates = await aggregate_upload.list_candidates(landing.group_id)
    landing.candidates_at = now
    return landing.candidates


async def _pick_next(
    landing: _Landing, size: int, attempted: set[int]
) -> LandingCandidate | None:
    """挑下一个落点（**固定顺序**，不排序）。"""
    candidates = await _refresh_candidates(landing)
    for cand in candidates:
        if cand.driver_id in attempted:
            continue
        if aggregate_upload.candidate_ok(cand, size):
            return cand
    return None


async def _switch_landing(task_id: int, candidate: LandingCandidate) -> None:
    """换落点：重建上传上下文 + 写回快照。"""
    landing = _LANDINGS.get(task_id)
    ctx = _CONTEXTS.get(task_id)
    if landing is None or ctx is None:
        return
    driver, uploader = await _build_clients(candidate.driver_id)
    settings = get_settings()
    _CONTEXTS[task_id] = SyncContext(
        driver=driver,
        uploader=uploader,
        index=CloudDirIndex(driver, candidate.root_id),
        root=ctx.root,
        delete_retries=settings.upload_delete_verify_retries,
        delete_delay=settings.upload_delete_verify_delay_sec,
        delete_local=landing.delete_after_upload,
        conflict_policy=landing.conflict_policy,
        staged_lookup=_make_staged_lookup(landing),
        # 换盘后子目录前缀**跟着任务走**（新副盘的缓冲根下同名子目录）
        dir_prefix=landing.sub_path,
    )
    landing.driver_id = candidate.driver_id
    landing.driver_name = candidate.driver_name
    landing.root_id = candidate.root_id
    landing.root_path = candidate.root_path
    await _persist_landing(task_id, landing)
    runtime = get_runtime(task_id)
    runtime.log(
        f"🔀 已切换落点 → {candidate.driver_name}"
        f"（{candidate.root_path or candidate.root_id}）"
    )


async def _ensure_landing_ok(
    task_id: int, landing: _Landing, size: int, attempted: set[int]
) -> str | None:
    """上传前确认当前落点还可用（额度 / 测连 / 空间）。

    Returns:
        ``None`` = 可用；否则是不可用原因（该文件直接判失败，不白撞墙）。
    """
    await _refresh_candidates(landing)
    cand = landing.current_candidate()
    if cand is None:
        return None
    if aggregate_upload.candidate_ok(cand, size):
        return None
    nxt = await _pick_next(landing, size, attempted)
    if nxt is None:
        if cand.exhausted:
            # 本月额度已耗尽且没有别的候选 → 直接失败（不白撞一次墙）
            return "组内副盘本月上传额度均已耗尽"
        # 空间不足 / 测连不健康但没有别的候选 → 照传，让服务端报错
        return None
    await _switch_landing(task_id, nxt)
    return None


async def _register_after_upload(
    task_id: int, outcome: SyncOutcome, rel_path: str, size: int
) -> None:
    """上传成功后**登记归拢台账**（数据同步 ↔ 网盘聚合的联动点）。

    - 组级幂等命中（文件早就在台账里）→ 不重复登记
    - 撞写锁 → **降级记日志**（兜底扫描会补上），绝不影响上传结果
    """
    landing = _LANDINGS.get(task_id)
    if landing is None:
        return
    if outcome.staged_hit:
        return
    if outcome.status not in (
        RESULT_UPLOADED,
        RESULT_OVERWRITTEN,
        RESULT_UNCHANGED,
    ):
        return
    file_id = outcome.cloud_file_id
    if not file_id:
        return
    # 台账口径 = **相对副盘缓冲根**（归拢时按它映射到主盘目标目录）。
    # 任务级「目标子目录」是缓冲根下的真实前缀 → 必须带上，否则归拢会
    # 把文件搬到主盘目标目录的根下（丢掉子目录）。
    ctx = _CONTEXTS.get(task_id)
    rel_dir = (
        ctx.cloud_dir(parent_dir(rel_path))
        if ctx is not None
        else parent_dir(rel_path)
    )
    file_name = base_name(rel_path)
    runtime = get_runtime(task_id)
    try:
        result = await aggregate_staging.register_uploaded_file(
            group_id=landing.group_id,
            source_driver_id=landing.driver_id,
            remote_id=file_id,
            file_name=file_name,
            rel_dir=rel_dir,
            size_bytes=int(outcome.size or size),
        )
    except OperationalError as exc:
        # ⚠️ **不登记降级台账**（与「落点快照写回」不同）：这是**运行期**
        # 的偶发撞锁，且**可丢**——兜底扫描（每小时）会把漏登记的文件补上。
        # 登记它会让面板长期显示 degraded（台账要等下一次成功才清），
        # 反而淹没真正的降级。
        runtime.log(
            f"⚠️ 归拢台账登记失败（写库繁忙）：{exc}"
            "——稍后由聚合作业的兜底扫描补登记"
        )
        return
    except Exception as exc:
        logger.warning("归拢台账登记失败：%s", exc)
        runtime.log(f"⚠️ 归拢台账登记失败：{exc}")
        return
    if result == "added":
        landing.staged_index[(rel_dir, file_name)] = aggregate_staging.StagedEntry(
            size_bytes=int(outcome.size or size),
            file_id=file_id,
            driver_id=landing.driver_id,
            state="pending",
        )
        runtime.log(f"📋 已登记归拢台账：{rel_path}")


async def _upload_with_switch(
    task_id: int,
    ctx: SyncContext,
    path: Path,
    rel_path: str,
    size: int,
    *,
    on_progress: Any = None,
) -> SyncOutcome:
    """传一个文件（聚合模式带**撞墙换盘重试**）。

    普通任务就是一次 :func:`sync_file`。聚合模式：

    1. 预检落点（额度 / 测连 / 空间）——不行就换盘
    2. 传；失败且命中**限额类**文案 → 标记「本月耗尽」→ 换盘 → **重试**
    3. 全部候选都撞墙 → 失败并点名

    Args:
        ctx: 调用方手上的上下文。**换盘后**以 ``_CONTEXTS`` 里的新上下文
            为准（这里传的会过时）。
    """
    landing = _LANDINGS.get(task_id)
    if landing is None:
        return await sync_file(ctx, path, rel_path, on_progress=on_progress)

    attempted: set[int] = set()
    while True:
        reason = await _ensure_landing_ok(task_id, landing, size, attempted)
        if reason:
            return SyncOutcome(RESULT_FAILED, rel_path, reason, size=size)
        current = _CONTEXTS.get(task_id) or ctx
        outcome = await sync_file(
            current, path, rel_path, on_progress=on_progress
        )
        if outcome.status != RESULT_FAILED:
            await _register_after_upload(task_id, outcome, rel_path, size)
            return outcome
        if not aggregate_upload.is_upload_quota_wall(outcome.reason):
            return outcome
        # 撞墙 → 标记「本月耗尽」→ 换盘 → 重试
        attempted.add(landing.driver_id)
        await aggregate_upload.mark_quota_exhausted(landing.driver_id)
        await _refresh_candidates(landing, force=True)
        nxt = await _pick_next(landing, size, attempted)
        if nxt is None:
            outcome.reason = f"{outcome.reason}｜组内副盘本月上传额度均已耗尽"
            return outcome
        await _switch_landing(task_id, nxt)


async def _worker_loop(
    task_id: int,
    queue: asyncio.Queue[_Item],
    ctx: SyncContext,
    *,
    gate: _SmallGate | None = None,
) -> None:
    """大文件 worker 主循环（每任务**恒 1 个**，内部串行）。

    **为什么恒 1 个**：瓶颈是**本机上行带宽**（~3.2 MB/s），
    并发换不来吞吐——单条 TCP 流已吃满上行，并发只是重新分配带宽，
    反而让服务端不稳（卡死 / ``Server disconnected``）。实测数据见
    ``docs/upload-bandwidth.md``。

    **降档信号**：大文件开始上传时置位闸的 ``big_active``——小文件通道
    自动降为 1，给大文件让路；传完复位。
    """
    while True:
        item = await queue.get()
        try:
            if gate is not None:
                await gate.set_big_active(True)
            try:
                await _process_one(
                    task_id, ctx, item.path, size=item.size, lane="large"
                )
            finally:
                if gate is not None:
                    await gate.set_big_active(False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("上传处理异常：%s", item.path)
        finally:
            queue.task_done()
            _drain_overflow(task_id)


async def _small_worker_loop(
    task_id: int,
    queue: asyncio.Queue[_Item],
    ctx: SyncContext,
    *,
    ordinal: int,
    gate: _SmallGate | None = None,
) -> None:
    """小文件 worker 主循环（K 个，K = 任务 ``concurrency`` 列）。

    并发受 :class:`_SmallGate` 动态调控（大文件上传中 / 熔断 → 1）。
    **不持** ``_SYNC_LOCKS``——「同文件双传」防护由 :data:`_INFLIGHT`
    承担；与全量同步/大文件通道并行正是双通道的意义（大文件一传
    几十分钟，小文件不能陪绑）。
    """
    while True:
        item = await queue.get()
        try:
            if gate is not None:
                await gate.acquire(ordinal)
            try:
                outcome = await _process_one(
                    task_id,
                    ctx,
                    item.path,
                    size=item.size,
                    use_lock=False,
                    lane="small",
                )
            finally:
                if gate is not None:
                    await gate.release(ordinal)
            if gate is not None and outcome is not None:
                _record_breaker(task_id, gate, outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("上传处理异常：%s", item.path)
        finally:
            queue.task_done()
            _drain_overflow(task_id)


def _record_breaker(task_id: int, gate: _SmallGate, outcome: SyncOutcome) -> None:
    """把单文件结果喂给熔断器（小文件 worker 专用）。"""
    if outcome.status != RESULT_FAILED:
        gate.record_success()
        return
    if gate.record_failure():
        runtime = get_runtime(task_id)
        runtime.log(
            f"⚠️ 小文件通道连续失败 {_BREAKER_FAIL_STREAK} 次，"
            f"熔断降为串行（冷却 {_BREAKER_COOLDOWN_SEC:.0f}s，"
            f"之后连续成功 {_BREAKER_RECOVER_SUCCESS} 次才恢复并发）"
        )
        runtime.log(f"   触发失败：{outcome.rel_path}（{outcome.reason}）")


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


async def _process_one(
    task_id: int,
    ctx: SyncContext,
    path: Path,
    *,
    size: int = -1,
    use_lock: bool = True,
    lane: str = "large",
) -> SyncOutcome | None:
    """处理一个文件（比对 → 决策 → 传）。

    Args:
        size: 入队时采到的文件大小。**只用于进度显示**——权威大小由
            :func:`sync_file` 现场比对。<0 = 现场采（放线程池，CIFS 规则）。
        use_lock: 是否持 ``_SYNC_LOCKS``——大文件通道与全量同步互斥；
            小文件通道不持（见 :data:`_INFLIGHT` 注释）。
        lane: ``large`` / ``small``——决定写哪个「正在上传」字段。

    Returns:
        同步结果；被过滤等提前返回时为 ``None``。
    """
    runtime = get_runtime(task_id)
    rel_path = relative_to_root(path, ctx.root)
    inflight = _INFLIGHT.setdefault(task_id, set())
    try:
        reason = should_skip(path, root=ctx.root)
        if reason is not None:
            runtime.log(f"⏭️ 跳过 {path.name}（{reason}）")
            return None

        if lane == "small":
            runtime.current_small = rel_path
        else:
            runtime.current = rel_path
        if size < 0:
            # ⚠️ **放线程池**：监听根在 CIFS 上，挂载卡死时同步 stat
            # 会无限等待并卡死整个事件循环（2026-11-06 prod 事故）。
            try:
                stat = await asyncio.to_thread(path.stat)
                file_size = stat.st_size
            except OSError:
                file_size = 0
        else:
            file_size = size

        # **大文件通道与全量同步互斥**：同一时刻只有一个协程调 sync_file。
        # 全量同步跑时 worker 在此等待——新事件不丢，只是延后处理。
        # ``lock`` 可能为 None（测试环境直接调本函数，未走 start_watcher）→
        # 退化为不互斥（测试本就是单协程串行）。
        lock = _SYNC_LOCKS.get(task_id) if use_lock else None
        if lock is not None:
            async with lock:
                outcome = await _upload_with_switch(
                    task_id,
                    ctx,
                    path,
                    rel_path,
                    file_size,
                    on_progress=_make_progress(runtime, rel_path, file_size),
                )
        else:
            outcome = await _upload_with_switch(
                task_id,
                ctx,
                path,
                rel_path,
                file_size,
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
        return outcome
    finally:
        inflight.discard(rel_path)


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

    **与事件流互斥靠 ``_INFLIGHT``**：扫描到「已在队列 / 处理中」的
    文件直接跳过（它会被对应通道传上去）；反之事件 dispatch 也会因
    全量同步正在传而跳过。**没有这层互斥，同一文件会被两边同时上传**
    ——光鸭同名上传自动改名，产出 ``xxx(1).bin`` 垃圾副本（实测行为）。
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
        skipped_inflight = 0
        # **与事件流 worker 互斥**：同一时刻只有一个协程调 sync_file。
        # 全量同步跑时大文件 worker 在 _process_one 里等锁——新事件不丢，
        # 只是延后。小文件通道不持锁（靠 _INFLIGHT 防重传）。
        # ``lock`` 可能为 None（测试环境直接调本函数，未走 start_watcher）→
        # 退化为不互斥（测试本就是单协程串行）。
        lock = _SYNC_LOCKS.get(task_id)
        inflight = _INFLIGHT.setdefault(task_id, set())
        overflow_box = _OVERFLOW.setdefault(task_id, {})
        index = 0
        async for path in aiter_local_files(
            watcher.root, include_subdirs=watcher.include_subdirs
        ):
            index += 1
            rel = watcher.relative(path)
            # **在传的文件跳过**——对应的通道正在处理，这里再传一遍就是
            # 「同文件双传」（见模块 docstring 的 xxx(1).bin 事故）。
            if rel in inflight:
                skipped_inflight += 1
                runtime.log(f"⏭️ [{index}] 跳过（事件队列处理中）：{rel}")
                continue
            inflight.add(rel)
            overflow_box.pop(rel, None)  # 全量同步接手 → 溢出暂存里那份作废
            runtime.current = rel
            # **每个文件都记一行** —— 大文件上传要几分钟，没有进度
            # 用户会以为卡死了。格式与事件驱动的路径一致。
            runtime.log(f"⏳ [{index}] 处理中：{rel}")
            try:
                # ⚠️ **放线程池**：CIFS 规则（挂载卡死时同步 stat 无限等待）。
                try:
                    stat = await asyncio.to_thread(path.stat)
                    file_size = stat.st_size
                except OSError:
                    file_size = 0
                if lock is not None:
                    async with lock:
                        outcome = await _upload_with_switch(
                            task_id,
                            ctx,
                            path,
                            rel,
                            file_size,
                            on_progress=_make_progress(runtime, rel, file_size),
                        )
                else:
                    outcome = await _upload_with_switch(
                        task_id,
                        ctx,
                        path,
                        rel,
                        file_size,
                        on_progress=_make_progress(runtime, rel, file_size),
                    )
            finally:
                inflight.discard(rel)
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
            + (f"，在传跳过 {skipped_inflight}" if skipped_inflight else "")
        )
        return {
            "uploaded": uploaded,
            "overwritten": overwritten,
            "unchanged": unchanged,
            "failed": failed,
            "conflicts": conflicts,
            "deleted_local": deleted_local,
            "uploaded_bytes": total_bytes,
            "skipped_inflight": skipped_inflight,
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
