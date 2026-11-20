"""迁移引擎：把源存储源的文件搬到光鸭（一次性手动同步）。

支持两种源：

- **115**（历史路径）：115 直链的 ``f=1`` 绑定「换链时用的 UA」。光鸭下载器
  有固定 UA（:data:`app.adapters.drivers.guangya.GUANGYA_DOWNLOAD_UA`），
  用它换链后交给光鸭「云下载」→ 数据走 ``115 CDN → 光鸭服务器``，不占本地
  磁盘/带宽。115 直链的 URL 末段是**文件名**，故落盘名天然正确。
- **光鸭**（2026-11-13 新增）：光鸭直链**不绑 UA**，云下载落盘名取自 URL
  末段——光鸭 CDN 直链末段是 ``download``（实测），故**必须改名**回原名。
  光鸭源的好处是**能搬被分享审核拒绝的文件**（``shareAuditStatus=3``
  的影视文件分享不出去，但直链+云下载完全可用，336MB 实测 7 秒落盘）。

**流式**：不预先跑一遍完整目录树。生产者用 ``iter_tree_by_cid`` 实时爬，
爬到一个合格文件就塞进有界队列；N 个消费者并发处理。所以：

- 115 的 1s/目录限速被光鸭下载时间掩盖，不构成瓶颈
- 开跑时**不知道总数**，进度是「已发现 / 已完成」而不是百分比

**幂等**（支持重复执行）：提交前先查光鸭目标目录，同名 + 同大小 →
`skipped_existing`。目标目录就是唯一真相源，不需要任何状态表。

**光鸭源的额外约束**（实测得出）：

- **恒串行**（并发 1）——落盘名都是 URL 末段，同目录并发提交会触发服务端
  自动改名 ``download(1)``，改名完成前不能提交下一个
- **提交间隔收紧到 5s**——光鸭源没有 115 的下载频控，瓶颈是云添加额度
- **改名后仍按「同名同大小」校验**——目标目录仍是唯一真相源

**中断恢复**：上一轮提交的任务可能还在光鸭服务端下载，目标目录里还没有
文件。故启动时拉一次活跃任务列表，从 ``res`` 链接的 ``d=`` 参数里解析
115 pickcode，命中则记 `skipped_inflight`（不提交、不等待）——下一轮由
目标目录预检自然收尾。光鸭源没有 pickcode，改用「认领残留」：目标目录里
若留着上一轮已落盘但没改名的 ``download`` 文件，按大小认领并改名。

**只看状态**：任务结束 = 从活跃列表消失（``cloud_task_list`` 默认不含
完成态，这里只拉 0/1）。不设任何超时——`get_task_status(taskId)` 对在途
任务返回 ``code 145 任务不存在``，不可用；卡死的任务只能靠页面「取消」。
"""

from __future__ import annotations

import asyncio
import posixpath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.adapters.drivers.base import CloudFile, DriverError
from app.adapters.drivers.guangya import GUANGYA_DOWNLOAD_UA
from app.core.logging import get_logger
from app.db.models import Driver
from app.db.session import session_scope
from app.domain.driver_types import ROOT_DIR_IDS
from app.services import naming
from app.services.driver_factory import build_driver
from app.services.migration_pipeline import (
    GUANGYA_LANDED_NAME,
    SubmitGate,
    TargetIndex,
    TaskWatcher,
)
from app.services.migration_pipeline import (
    norm_dir as _norm_dir,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from app.services.migration_jobs import MigrationJob

logger = get_logger(__name__)

#: 兼容别名——管线件已抽到 :mod:`app.services.migration_pipeline`（归拢引擎共用），
#: 但本模块历史上以私有名暴露它们，外部引用与测试仍按旧名访问。
_TargetIndex = TargetIndex
_SubmitGate = SubmitGate

#: 轮询离线任务状态的间隔（秒）。
#:
#: 一个共享 poller 服务所有并发 worker——不管开多少并发，都是每 N 秒一次
#: 请求，不会因为并发数把光鸭打爆（限速 0.2s/次）。
#:
#: 实际取值来自配置 ``MIGRATION_POLL_INTERVAL_SEC``（默认 5 秒）；这里是
#: **兜底默认值**，配置读不到时用它。
#:
#: 注：轮询器实现已抽到 :mod:`app.services.migration_pipeline`（归拢引擎
#: 共用），但**本模块保留这个常量**——测试会 monkeypatch 它来加速轮询。
POLL_INTERVAL_SEC = 5.0

#: 默认的「提交间隔」（秒）——两次给光鸭提交离线任务之间至少隔这么久。
#:
#: **为什么需要**：115 对「单位时间内拉取不同文件」有频控。实测并发提交时
#: 直链会返回 ``403 {"message":"115 pmt user 11-10"}``（数字随请求次数
#: 递增，末尾固定 ``-10``），光鸭拉不到就记「云添加失败 code=8」。
#:
#: 错开提交是**唯一可靠的对策**——重试只会再撞一次。
DEFAULT_SUBMIT_INTERVAL_SEC = 60.0

#: 任务结束后校验落盘的重试次数与间隔。
#:
#: 文件索引有延迟：刚完成时目录里可能还查不到，验早了会误报失败。
#:
#: **单一真源在** :mod:`app.services.migration_pipeline`（归拢引擎共用），
#: 此处再导出一份是为了让本模块的调用点读起来自洽。
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

#: 允许作为**源**的存储源类型。
#:
#: - ``p115``：历史路径（直链绑 UA，落盘名正确）
#: - ``guangya``：新增——光鸭 → 光鸭（直链不绑 UA，落盘名需改名）
SOURCE_DRIVER_TYPES = frozenset({"p115", "guangya"})

#: 光鸭源的最大提交间隔（秒）。
#:
#: 115 源需要 60s 间隔是因为 115 对「短时间拉取不同文件」有频控
#: （403 ``115 pmt user 11-10``）；光鸭源没有这个限制，瓶颈是云添加
#: 额度，故间隔自动收紧——用户显式设更小的值照用，设 0（不限速）也照用。
GUANGYA_SUBMIT_INTERVAL_MAX = 5.0

#: 迁移的并发数——**恒为 1**（迁移**不支持并发**，2026-11-15 决策）。
#:
#: 云下载的落盘名取自 URL 末段（光鸭直链末段 = ``download``），同目录
#: 并发提交会触发服务端自动改名 ``download(1)``，改名完成前不能提交
#: 下一个。额度也是瓶颈，并行无收益。
MIGRATION_CONCURRENCY = 1

#: 云添加额度耗尽的文案特征（命中即暂停，不硬撞不重试）。
#:
#: 实测文案：「云添加次数超限，下个自然日刷新」。额度当天不恢复，重试
#: 只是白扣（**失败的也扣额度**），故撞上就暂停整个作业——幂等设计保证
#: 次日重跑会自动续上。
QUOTA_ERROR_MARKERS = ("次数超限", "额度", "配额", "每日上限")


class MigrationError(RuntimeError):
    """迁移无法开始（配置错误 / 驱动不可用）。"""


class MigrationQuotaExceeded(MigrationError):
    """云添加额度耗尽——作业暂停（当日不恢复，次日重跑即可续传）。"""


def _is_quota_error(message: str) -> bool:
    """错误文案里是否含「额度耗尽」特征。"""
    text = str(message or "")
    return any(marker in text for marker in QUOTA_ERROR_MARKERS)


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
            await db.execute(select(Driver).where(Driver.id == job.params.target_driver_id))
        ).scalar_one_or_none()

    if source is None or target is None:
        raise MigrationError("存储源不存在")
    if source.type not in SOURCE_DRIVER_TYPES:
        raise MigrationError(
            f"源存储源类型不支持迁移（当前：{source.type}，"
            f"支持：{'、'.join(sorted(SOURCE_DRIVER_TYPES))}）"
        )
    if target.type != "guangya":
        raise MigrationError(f"目标存储源必须是光鸭盘（当前：{target.type}）")
    if source.id == target.id:
        raise MigrationError("源与目标是同一个存储源")

    async def _on_refresh(cred: Any) -> None:
        await persist_guangya_credential(job.params.target_driver_id, cred)

    async def _on_refresh_source(cred: Any) -> None:
        await persist_guangya_credential(params.source_driver_id, cred)

    #: 光鸭源：直链不绑 UA、落盘名要改名、必须串行
    needs_rename = source.type == "guangya"
    if needs_rename:
        source_driver = build_driver(
            "guangya",
            source.credential,
            source.device_type,
            on_token_refresh=_on_refresh_source,
        )
    else:
        source_driver = build_driver("p115", source.credential, source.device_type)
    gyp = build_driver(
        "guangya",
        target.credential,
        target.device_type,
        on_token_refresh=_on_refresh,
    )

    # 源目录路径：以 cid 反解的路径为准（前端传来的路径可能过期）
    base_path = (params.source_path or "/").rstrip("/") or "/"
    resolver = getattr(source_driver, "resolve_path", None)
    if resolver is not None:
        try:
            resolved = await resolver(params.source_cid)
            if resolved:
                base_path = str(resolved).rstrip("/") or "/"
        except Exception as exc:
            job.log(f"⚠️ 源目录路径解析失败，沿用前端传入的路径：{exc}")
    elif needs_rename:
        # 光鸭接口不返回路径字符串——用前端传来的目录名链拼成绝对路径
        # （前端给的是「影视/电视剧」这种无首斜杠的相对串）
        base_path = "/" + params.source_path.strip("/")
    job.source_path = base_path

    # **恒串行**（2026-11-15 决策）：迁移**不支持并发**。
    #
    # 为什么串行是对的：
    # - 云下载的落盘名取自 URL 末段（光鸭直链 = ``download``），同目录并发
    #   提交会触发服务端自动改名 ``download(1)``，改名完成前不能提交下一个
    # - 云添加额度稀缺（光鸭实测 3 次/账号/天）——并发提交只会更快撞墙
    # - 115 源另有下载频控（403 ``115 pmt user 11-10``），错开提交是唯一
    #   可靠对策
    #
    # 提交间隔仍按源类型收紧：115 源要 60s（避开下载频控），光鸭源 5s。
    concurrency = MIGRATION_CONCURRENCY
    submit_interval = params.submit_interval_sec
    if needs_rename and submit_interval > 0:
        submit_interval = min(submit_interval, GUANGYA_SUBMIT_INTERVAL_MAX)

    # 「搬完清理源」：**只有光鸭源支持**（115 驱动没有删除能力）
    delete_source = bool(params.delete_source)
    if delete_source and not needs_rename:
        delete_source = False
        job.log("⚠️ 115 源不支持清理源（驱动无删除能力），本次只迁移不删除")
    elif delete_source:
        job.log(
            "🗑️ 已开启「搬完清理源」：**只删目标侧已确认落盘同名同大小的文件**，"
            "删除进回收站（可恢复）"
        )

    # 上一轮遗留的在途任务（中断恢复）
    inflight = await _collect_inflight(gyp, job)
    job.legacy_inflight = len(inflight)

    video_exts = {
        e.strip().lower() for e in (source.video_exts or "").split(",") if e.strip()
    }
    min_bytes = max(params.min_size_mb, 0) * 1024 * 1024
    max_bytes = max(params.max_size_mb, 0) * 1024 * 1024

    job.log(
        f"源 {base_path}（{source.name} / cid={params.source_cid}）"
        f"→ 目标 {params.target_path or '/'}（{target.name}）"
        f"；阈值 ≥{params.min_size_mb} MB，串行提交"
        f"，提交间隔 {submit_interval:g}s"
    )
    if needs_rename:
        job.log(
            "ℹ️ 光鸭源：直链不绑 UA，云下载按 URL 末段落名（download）"
            "→ 落盘后自动改名回原名；恒串行（同目录并发会撞名）"
        )
    if inflight:
        job.log(
            f"检测到 {len(inflight)} 个上一轮遗留的在途任务，本轮跳过对应文件"
            f"（光鸭上实际在跑 {len(inflight) + concurrency} 个）"
        )
        if len(inflight) >= 5:
            job.log(
                "⚠️ 遗留任务较多——它们同样在消耗 115 的下载频控配额，"
                "建议等它们跑完再开新任务（否则更容易撞上「云添加失败 code=8」）"
            )

    queue: asyncio.Queue[CloudFile] = asyncio.Queue(maxsize=max(concurrency * 2, 4))
    index = TargetIndex(gyp, params.target_file_id)
    watcher = TaskWatcher(gyp, log=job.log, poll_interval=POLL_INTERVAL_SEC)
    gate = SubmitGate(submit_interval)

    producer: asyncio.Task[None] | None = None
    workers: list[asyncio.Task[None]] = []
    try:
        producer = asyncio.create_task(
            _produce(
                job,
                source_driver,
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
                    source=source_driver,
                    index=index,
                    watcher=watcher,
                    gate=gate,
                    queue=queue,
                    inflight=inflight,
                    base_path=base_path,
                    needs_rename=needs_rename,
                    delete_source=delete_source,
                )
            )
            for _ in range(concurrency)
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
    """实时爬源目录树，把合格文件塞进队列。"""
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
    source: Any,
    index: TargetIndex,
    watcher: TaskWatcher,
    gate: SubmitGate,
    queue: asyncio.Queue[CloudFile],
    inflight: set[str],
    base_path: str,
    needs_rename: bool,
    delete_source: bool = False,
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
                source=source,
                index=index,
                watcher=watcher,
                gate=gate,
                inflight=inflight,
                base_path=base_path,
                needs_rename=needs_rename,
                delete_source=delete_source,
            )
        except asyncio.CancelledError:
            raise
        except MigrationQuotaExceeded as exc:
            # 额度耗尽：**暂停整个作业**（当日不恢复，重试只是白扣额度）。
            # 必须把队列排空——否则生产者会卡在 put 上，作业永远收不了尾。
            job.pause(str(exc))
            _drain_queue(queue)
            return
        except Exception as exc:  # 任何异常 = 跳过这个文件
            job.record_failed(
                entry, f"{type(exc).__name__}: {exc}"
            )
            logger.warning("迁移失败 %s：%s", entry.path, exc)
        finally:
            queue.task_done()


def _drain_queue(queue: asyncio.Queue[CloudFile]) -> None:
    """把队列里剩余的条目丢弃（配合 ``task_done`` 让 ``join`` 能返回）。"""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        else:
            queue.task_done()


async def _migrate_one(
    job: MigrationJob,
    entry: CloudFile,
    *,
    source: Any,
    index: TargetIndex,
    watcher: TaskWatcher,
    gate: SubmitGate,
    inflight: set[str],
    base_path: str,
    needs_rename: bool,
    delete_source: bool = False,
) -> None:
    """处理单个文件：预检 → 换链 → 提交 → 等结束 → 改名 → 校验落盘。"""
    job.current_path = entry.path
    rel_dir = _relative_dir(entry.path, base_path)

    target_dir_id, existing = await index.resolve(rel_dir)

    # ① 目标已有同名文件 → **保留大的**（与数据同步的既有口径一致）：
    #    - 同名同大小 → 跳过（目标已有，源是冗余副本）
    #    - 目标更大   → 保留目标那份，跳过（源是冗余的小副本）
    #    - 源更大     → 删除目标侧的小文件，然后正常传（保留大的）
    remote_size = existing.get(entry.name)
    if remote_size is not None:
        if remote_size == entry.size:
            job.skipped_existing += 1
            job.log(f"⏭️ 已存在，跳过：{entry.path}")
            # 「搬完清理源」：目标已有**同名同大小** → 源副本冗余，一并清掉
            # （否则「跳过」会留下源副本，腾空间的目的就落空了）
            if delete_source:
                await _delete_source_file(job, source, entry)
            return
        if remote_size > entry.size:
            job.skipped_existing += 1
            job.log(
                f"⏭️ 同名但目标更大（目标 {remote_size:,} / 源 {entry.size:,} 字节），"
                f"保留目标那份，跳过：{entry.path}"
            )
            if delete_source:
                await _delete_source_file(job, source, entry)
            return
        # 源更大 → 删目标侧的小文件，保留大的（源那份）
        job.record_conflict(entry, remote_size)
        job.log(
            f"⚠️ 同名但源更大（源 {entry.size:,} / 目标 {remote_size:,} 字节）"
            f"→ 保留大的：删除目标侧旧文件并重传：{entry.path}"
        )
        try:
            removed = await index.delete_replaced(rel_dir, entry.name, remote_size)
        except Exception as exc:  # 删不掉就退回「不覆盖」
            job.log(f"⚠️ 删除目标侧旧文件失败（保留目标那份，不重传）：{exc}")
            return
        if not removed:
            job.log("⚠️ 目标侧旧文件已变化，跳过（不覆盖）")
            return
        job.log(f"🗑️ 已删除目标侧旧文件（{remote_size:,} 字节），改用源那份")

    # ①' 光鸭源：上一轮可能已落盘但没改名（改名前中断/被取消）→ 认领它，
    #     别重复下载（云添加额度稀缺，重复提交就是白扣）。
    if needs_rename:
        adopted = await index.rename_landed(
            rel_dir, (GUANGYA_LANDED_NAME,), entry.size, entry.name
        )
        if adopted:
            job.skipped_existing += 1
            job.log(f"♻️ 认领上一轮残留（已落盘未改名）：{entry.path}")
            # 认领后目标侧已是同名同大小 → 同「已存在」处理
            if delete_source:
                await _delete_source_file(job, source, entry)
            return

    # ② 上一轮还在下载（目标目录里还没有文件）
    if entry.remote_id in inflight:
        job.skipped_inflight += 1
        job.log(f"⏭️ 上一轮仍在下载，本轮跳过：{entry.path}")
        return

    # ③ 错开提交：115 对「短时间拉取不同文件」有频控（实测 403
    #    "115 pmt user 11-10"），光鸭拉不到就记「云添加失败 code=8」。
    #    **先过闸门再换链**——直链是新鲜的，光鸭收到就能开始拉。
    #
    waited = await gate.wait()
    if waited >= 1:
        job.log(f"⏱️ 错开提交，等待 {waited:.0f}s：{entry.path}")

    # ④ 换直链
    #    - 115 源：f=1 必须匹配光鸭下载器的 UA，否则 403
    #    - 光鸭源：**不绑 UA**（光鸭直链与 UA 无关）
    link = await source.get_direct_link(
        entry.remote_id, GUANGYA_DOWNLOAD_UA if not needs_rename else ""
    )
    if not link.url:
        raise DriverError("源存储源返回空直链", kind="not_found")
    job.log(f"🔗 已换取直链：{entry.path}")

    # ⑤ 提交云下载
    #
    # ⚠️ **额度靠撞墙判定**（没有账本——服务端不提供「剩余次数」查询）：
    # 撞到「次数超限 / 额度 / 配额 / 每日上限」字样 → 作业暂停，次日自动续传。
    # 泛化失败（「云添加失败」code=8）**认不出是不是额度** → 当普通失败，
    # 跳过该文件继续下一个（**不自动重试**——失败也扣额度）。
    try:
        task_id = await index.create_offline_task(link.url, target_dir_id)
    except DriverError as exc:
        if _is_quota_error(str(exc)):
            raise MigrationQuotaExceeded(str(exc)) from exc
        raise
    # 登记到缓存——否则同目录的后续预检看不到这个文件（缓存与实际不一致）
    await index.note_submitted(rel_dir, entry.name, entry.size)
    job.log(
        f"⬆️ 已提交：{entry.path}"
        f"（{entry.size / 1024**3:.2f} GB）"
    )

    job.in_flight += 1
    job.track_task(task_id, entry.path, entry.size)
    try:
        await watcher.wait(task_id, path=entry.path, size=entry.size)
    finally:
        job.in_flight -= 1
        job.untrack_task(task_id)

    # ⑤ 光鸭源：落盘名是 URL 末段（download）→ 改名回原名
    #
    # ⚠️ **跳过目录里原有的同名文件**（与聚合归拢同款保护）：用户在目标目录
    # 放了个叫 ``download`` 的文件时，服务端会把我们的落地文件自动改名成
    # ``download(1)`` → 跳过 ``download`` 后只改**我们那份**。
    # ``existing`` 是**提交前**的目录快照（含用户原有文件，不含本次落地）。
    if needs_rename:
        landed_names = (_landed_name_from_url(link.url), GUANGYA_LANDED_NAME)
        renamed = await index.rename_landed(
            rel_dir,
            tuple(n for n in landed_names if n),
            entry.size,
            entry.name,
            exclude_names=set(existing),
        )
        if renamed:
            job.log(f"✏️ 已改名：{renamed} → {entry.name}")

    # ⑥ 任务结束 → 以「目标目录里是否真的出现同名同大小文件」为准
    #    索引有延迟，验早了会误报，故重试几次
    for attempt in range(VERIFY_RETRIES):
        files = await index.refresh(rel_dir)
        if files.get(entry.name) == entry.size:
            job.success += 1
            job.migrated_bytes += entry.size
            job.log(f"✅ 完成：{entry.path}")
            # ⑦ 「搬完清理源」——**只在目标侧已确认落盘同名同大小**之后删。
            #    顺序不能反：先删源再传，一旦传失败就是数据没了。
            if delete_source:
                await _delete_source_file(job, source, entry)
            return
        if attempt + 1 < VERIFY_RETRIES:
            await asyncio.sleep(VERIFY_DELAY_SEC)

    reason = await index.failure_reason(task_id)
    if _is_quota_error(reason):
        raise MigrationQuotaExceeded(reason)
    job.record_failed(
        entry, reason or "任务已结束但目标目录未出现同名同大小文件"
    )


async def _delete_source_file(job: MigrationJob, source: Any, entry: CloudFile) -> None:
    """清理源端文件（**仅在显式开启「搬完清理源」时调用**）。

    - **只删已校验成功的**（调用点保证：目标侧已确认同名同大小落盘）
    - 走 ``fs_delete`` → **进回收站**，不是永久删除（可恢复）
    - **记审计日志**——删数据不可逆，必须能回答「删了什么」
    - 单个失败不中断其余条目（只记警告）
    """
    try:
        await source.delete_file(entry.remote_id)
    except Exception as exc:  # 清理失败不该炸掉整个迁移
        job.record_failed(entry, f"清理源失败：{exc}")
        logger.warning("迁移清理源失败 %s：%s", entry.path, exc)
        job.log(f"⚠️ 清理源失败：{entry.path}（{exc}）")
        return
    job.deleted_source += 1
    logger.warning("迁移清理源：删除 %s（%d 字节）", entry.path, entry.size)
    job.log(f"🗑️ 已清理源：{entry.path}")


def _landed_name_from_url(url: str) -> str:
    """从直链 URL 推导云下载的落盘名（取路径末段）。

    光鸭云下载按 URL 末段命名——115 直链末段是文件名（落名正确），
    光鸭 CDN 直链末段是 ``download``。取不到就返回空串。
    """
    if not url:
        return ""
    path = str(url).split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""


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
    """光鸭 Token 刷新后写回 DB（不写回则重启后 401）。

    真正的实现放在 :func:`app.services.driver_service.persist_guangya_credential`
    ——**合并**「Web 层 + 客户端覆盖层」后再写回（整串替换会把另一层抹掉，
    2026-11-20 生产事故）。这里保留本函数只是为了兼容既有调用点。

    **异常口径（与委托前的实现一致，没变）**：写回失败（DB 错误等）在底层
    :func:`app.services.driver_service.persist_guangya_credential` 里被捕获
    并记日志（``写回刷新的光鸭凭据失败``），**不向上抛**。本函数被
    ``_on_refresh`` / ``_on_refresh_source`` / ``_guangya_refresh_callback``
    当作 ``on_token_refresh`` 回调用，调用方依赖「写回失败不影响本次任务」
    这条约定；驱动侧 :meth:`GuangyaDriver._notify_token_refresh` 还有一层
    兜底（回调抛异常也只记日志）。**别改成向上抛**——理由与实测后果见
    :func:`app.services.driver_service.persist_guangya_credential`。

    Raises:
        无——写回失败只记日志，不中断调用链。
    """
    from app.services.driver_service import persist_guangya_credential as _persist

    await _persist(driver_id, cred)


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
    row = await _load_driver_row_any(driver_id)
    if row.type != expected_type:
        raise MigrationBrowseError(
            f"该接口需要 {expected_type} 存储源（当前：{row.type}）",
            status_code=400,
            kind="config",
        )
    return row


async def _load_driver_row_any(driver_id: int) -> Any:
    """取存储源行（**不校验类型**）。

    Raises:
        MigrationBrowseError: 存储源不存在（404）。
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
        return row


def _guangya_refresh_callback(driver_id: int) -> Any:
    """造一个「光鸭 Token 刷新后写回 DB」的回调（驱动构造用）。"""

    async def _cb(cred: Any) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


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
    """列出源目录下的子目录（逐层浏览）。

    两种源各按自己的 id 语义浏览：

    - **115**：按 ``cid``（目录 id 恒定，改名不失效）
    - **光鸭**：按 ``fileId``，**从账号根开始**（光鸭账号根 id 是空串，
      见 :data:`app.domain.driver_types.ROOT_DIR_IDS`）——光鸭账号可以
      没有配置媒体库根，迁移的源目录由用户在页面里逐层选

    返回 ``[(remote_id, name), ...]``——只含目录。
    """
    row = await _load_driver_row_any(driver_id)
    if row.type == "p115":
        cid = parent_id or _default_source_cid(row.media_cid)
        driver = build_driver(row.type, row.credential, row.device_type)
    elif row.type == "guangya":
        cid = parent_id or ROOT_DIR_IDS.get("guangya", "")
        driver = build_driver(
            row.type,
            row.credential,
            row.device_type,
            on_token_refresh=_guangya_refresh_callback(row.id),
        )
    else:
        raise MigrationBrowseError(
            f"该存储源类型不支持作为迁移源（当前：{row.type}）",
            status_code=400,
            kind="config",
        )
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
    """校验迁移的源 / 目标存储源。

    - 源：``p115`` 或 ``guangya``（光鸭 → 光鸭）
    - 目标：**必须是** ``guangya``
    - 源与目标**不能是同一个**（自己搬自己没意义，还会撞名）

    Raises:
        MigrationBrowseError: 存储源不存在（404）/ 类型不符（400）/ 同源（400）。
    """
    source = await _load_driver_row_any(source_driver_id)
    if source.type not in SOURCE_DRIVER_TYPES:
        raise MigrationBrowseError(
            f"源存储源类型不支持迁移（当前：{source.type}，"
            f"支持：{'、'.join(sorted(SOURCE_DRIVER_TYPES))}）",
            status_code=400,
            kind="config",
        )
    await _load_driver_row(target_driver_id, "guangya")
    if source_driver_id == target_driver_id:
        raise MigrationBrowseError(
            "源与目标是同一个存储源", status_code=400, kind="config"
        )

