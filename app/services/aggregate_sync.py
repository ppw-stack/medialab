"""归拢引擎：把副数据源上的文件搬回主数据源（services 层）。

## 一句话

副盘是缓冲，主盘是唯一真源。本模块把「在途台账」里的文件**逐个**搬到主盘，
主盘确认落盘后**自动清理副盘副本**。

## 链路（与手动迁移同一条，见 :mod:`app.services.migration_pipeline`）

```
取直链 → 提交云下载（光鸭 → 光鸭，服务端内部复制）→ 等任务结束
→ 改名（落盘名是 download）→ 校验同名同大小 → 清理副盘副本
```

## 三条硬约束（实测结论，不是偏好）

1. **云添加额度 3 次/账号/天，失败也扣** → **撞墙探测**（服务端不提供剩余
   次数查询）：撞到「次数超限」类文案 → 整组转 ``paused``（不硬撞不重试），
   次日额度窗口打开时自动续传。
2. **恒串行**（并发 1）——落盘名撞车（``download(1)``）+ 额度都是瓶颈。
3. **副盘副本只在主盘确认落盘后清理**——顺序不可反：先删源再传，
   一旦传失败就是数据没了。

## 中断恢复

- ``syncing`` 的行：先查光鸭活跃任务列表，任务**还在跑** → 本轮跳过；
  任务**已消失** → 重置为待归拢（若已落盘没改名，认领残留那一步会接住）
- 上一轮「已落盘但没改名」的 ``download`` 文件 → 按字节数认领并改名
  （**不重复下载**——重复提交就是白扣额度）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.adapters.drivers.base import DriverError
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import AggregateGroup, Driver, SecondaryStaging
from app.db.session import session_scope
from app.domain.aggregate_types import (
    ROLE_MASTER,
    ROLE_SECONDARY,
    STAGING_DONE,
    STAGING_FAILED,
    STAGING_PAUSED,
    STAGING_PENDING,
    STAGING_SYNCING,
    STAGING_VERIFIED,
)
from app.services.aggregate_staging import enqueue_from_staging_root
from app.services.driver_factory import build_driver
from app.services.errors import ServiceError
from app.services.migration import persist_guangya_credential
from app.services.migration_pipeline import (
    GUANGYA_LANDED_NAME,
    VERIFY_DELAY_SEC,
    VERIFY_RETRIES,
    SubmitGate,
    TargetIndex,
    TaskWatcher,
)

logger = get_logger(__name__)

__all__ = [
    "AggregateSyncResult",
    "run_aggregate_sync",
    "run_group",
    "sync_group_now",
]

#: 光鸭源的提交间隔（秒）——云添加额度稀缺，**慢一点比撞限额好**。
#:
#: 与手动迁移同款（光鸭源收紧到 5s），但这里**恒为 5s**：归拢是后台任务，
#: 不赶时间，撞限额才是真损失。
SUBMIT_INTERVAL_SEC = 5.0

#: 同一轮的**最大提交数**——**纯运行时保护**（一轮跑太久会占着事件循环不放）。
#:
#: ⚠️ **它不是额度限制**：额度由**服务端**说了算（撞墙即停），这里只防单轮
#: 跑飞。
#:
#: **可调**：运行时参数 ``aggregate_max_submissions``（默认 20，0 = 不限）。
#: ⚠️ 这是**兜底默认值**——参数读不到时用它；正常路径读 DB 里的值
#: （作业每次执行前读最新值，无缓存 / 即时一致）。
MAX_SUBMISSIONS_PER_RUN = 20

#: 运行时参数 key（单轮提交上限；0 = 不限）
MAX_SUBMISSIONS_PARAM = "aggregate_max_submissions"

#: 运行时参数 key（小文件走「分享转存」的体积上限；0 = 关闭该通道）
SHARE_THRESHOLD_PARAM = "aggregate_share_threshold_bytes"

#: 小文件通道的默认体积上限（10 MB）。
#:
#: 附件（nfo/jpg/png）是 KB 级、视频是 GB 级 → 10 MB 分界余量充足。
SHARE_THRESHOLD_DEFAULT = 10 * 1024 * 1024

#: 分享转存通道：等文件出现在目标目录的上限（秒）。
#:
#: 转存是**异步任务**（返回 taskId），但**没有可靠的完成查询接口**
#: （``get_task_status`` 是上传任务的接口，查云下载任务返回 145）→ 只能
#: **列目录轮询**（与「校验落盘」同一条判据）。
SHARE_TRANSFER_WAIT_SEC = 600.0

#: 分享转存通道：列目录轮询间隔（秒）
SHARE_TRANSFER_POLL_SEC = 3.0

#: ⚠️ **失败的文件不自动重试**（2026-11-15 决策）
#:
#: 光鸭撞额度时**可能只回泛化的「云添加失败」**（无「次数超限」字样），
#: 认不出是额度问题；而**失败也扣额度** → 自动重试就是白扣，且永远等不到
#: 「自动续传」。故泛化失败一律记 ``failed``，**只由人工点「重试」**。
#:
#: 注意：**暂停**（``paused``，文案明确是额度耗尽）**仍会自动续传**——
#: 次日额度窗口打开，作业把 ``paused`` 行捡起来继续。

#: 云添加额度耗尽的文案特征（命中即暂停，不硬撞不重试）。
QUOTA_ERROR_MARKERS = ("次数超限", "额度", "配额", "每日上限")

#: 单个云下载任务的**最大等待时长**（秒）——2 小时。
#:
#: ⚠️ 光鸭已知会把任务**卡在活跃列表不结束**（AGENTS.md 实测：15MB 任务
#: 卡住）。无超时的话 run 永不结束 → APScheduler ``max_instances=1`` →
#: 后续 hourly 触发全部跳过 → **不止本组，其它组的归拢也全部饿死**。
#: 超时把该行标 ``failed``（人工重试），run 继续下一行。
#:
#: 注意：超时**不代表任务失败**——它可能仍在服务端下载，甚至最终落盘。
#: 故 ``cloud_task_id`` 保留，人工重试时「认领残留」能接住已落盘的情况。
TASK_MAX_WAIT_SEC = 2 * 3600.0


@dataclass(slots=True)
class AggregateSyncResult:
    """一轮归拢的结果（作业返回值 / API 响应）。"""

    groups: int = 0
    #: 处理成功的文件数
    synced: int = 0
    #: 已清理副盘副本的文件数
    cleaned: int = 0
    #: 因额度耗尽暂停的文件数
    paused: int = 0
    #: 失败的文件数
    failed: int = 0
    #: 跳过的文件数（已在途 / 认领了残留）
    skipped: int = 0
    #: 逐条明细（文件名 → 结果说明）
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": self.groups,
            "synced": self.synced,
            "cleaned": self.cleaned,
            "paused": self.paused,
            "failed": self.failed,
            "skipped": self.skipped,
            "details": list(self.details),
        }


class _QuotaExceeded(Exception):
    """云添加额度耗尽——本组转 ``paused``（当日不恢复，次日续传）。"""


def _is_quota_error(message: str) -> bool:
    """错误文案里是否含「额度耗尽」特征。"""
    text = str(message or "")
    return any(marker in text for marker in QUOTA_ERROR_MARKERS)


def _landed_name_from_url(url: str) -> str:
    """从直链 URL 推导云下载的落盘名（取路径末段）。

    光鸭云下载按 URL 末段命名——光鸭 CDN 直链末段是 ``download``（实测）。
    """
    if not url:
        return ""
    path = str(url).split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""


def _landed_candidate_names(url: str = "") -> tuple[str, ...]:
    """落盘名候选**全集**（不做任何排除）。

    云下载落盘名取自 URL 末段（光鸭直链 = ``download``），撞名时服务端会
    自动改成 ``download(1)``（:func:`app.services.migration_pipeline.TargetIndex.
    rename_landed` 已支持该形态）。
    """
    names = {GUANGYA_LANDED_NAME}
    derived = _landed_name_from_url(url)
    if derived:
        names.add(derived)
    return tuple(sorted(name for name in names if name))


def _landed_candidates_for_submit(existing: dict[str, int], url: str) -> tuple[str, ...]:
    """**提交后改名**用的候选（不排除任何名字）。

    「保护用户原有文件」靠 :func:`rename_landed` 的 ``exclude_names``
    **精确跳过同名条目**——而不是把候选名整个删掉。

    ⚠️ **为什么不能删候选**：用户在目标目录放了叫 ``download`` 的文件时，
    服务端会把我们的落地文件自动改名成 ``download(1)``。若把候选 ``download``
    整个排除，``download(1)`` 也认不出来 → **这个目录的归拢必然失败**。
    故候选保持完整，改名时只跳过「目录里原有的同名条目」。
    """
    return _landed_candidate_names(url)


def _refresh_callback(driver_id: int) -> Any:
    async def _cb(cred: Any) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


# --------------------------------------------------------------------------
# 组级归拢锁（**归拢全程串行**的并发保护）
# --------------------------------------------------------------------------
#
# 为什么需要：组内串行、组间串行都只保护**单次调用内部**——手动
# ``POST /groups/{id}/sync`` 与每小时 cron 作业可重叠（APScheduler 的
# ``max_instances=1`` 只防作业自身重叠，防不了 API 触发），连点两次按钮
# 也一样。两个 run 各自 ``_load_processable_rows`` 拿到同一批 pending 行
# → 同一文件被提交两次（**白扣额度**）。本应用是单进程，进程内锁足够。

_GROUP_LOCKS: dict[int, asyncio.Lock] = {}
_LOCKS_GUARD = asyncio.Lock()


def forget_group_lock(group_id: int) -> None:
    """忘掉某组的归拢锁（**组被解散时**调用）。

    锁字典是进程级的、只增不减——组的数量是固定的（个位数），但「建组 →
    解散 → 建组」反复循环会让字典慢慢长。组没了，锁就该一起走。
    """
    _GROUP_LOCKS.pop(group_id, None)


async def _group_lock(group_id: int) -> asyncio.Lock:
    """取某组的归拢锁（惰性创建；创建本身也要防竞态）。

    锁随组的删除**不回收**——组是极少量对象（个位数），泄漏量可忽略；
    回收逻辑反而引入「锁在别人手里被清掉」的新竞态。
    """
    async with _LOCKS_GUARD:
        lock = _GROUP_LOCKS.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            _GROUP_LOCKS[group_id] = lock
        return lock


# --------------------------------------------------------------------------
# 作业入口
# --------------------------------------------------------------------------


async def run_aggregate_sync() -> dict[str, Any]:
    """跑一轮归拢（调度作业入口）。

    **组间串行**：光鸭的限速是账号级，组间并发并不能提高总吞吐，
    反而让单次作业的耗时不可预测。组内更必须串行（落盘名撞车）。
    """
    result = AggregateSyncResult()
    # 单轮提交上限 = **运行时参数**（DB 持久化、面板可调；0 = 不限）
    max_submissions = _scheduled_max_submissions()
    async with session_scope() as db:
        groups = (
            (await db.execute(select(AggregateGroup).order_by(AggregateGroup.id)))
            .scalars()
            .all()
        )
        group_ids = [(int(g.id), g.name, bool(g.enabled)) for g in groups]

    for group_id, group_name, enabled in group_ids:
        if not enabled:
            continue
        result.groups += 1
        try:
            # 定时作业：**限 20 次**（无人值守的运行时保护）+ 锁被占就跳过
            # （手动触发正在跑时不必排队干等——下一轮自然会补）
            await run_group(
                group_id,
                result,
                max_submissions=max_submissions,
                wait_for_lock=False,
            )
        except Exception as exc:  # 单组失败不影响其它组
            logger.exception("聚合组 %s 归拢失败", group_name)
            result.failed += 1
            result.details.append(f"组「{group_name}」失败：{exc}")
    return result.to_dict()


def _share_threshold_bytes() -> int:
    """小文件走「分享转存」的体积上限（**运行时参数**，0 = 关闭该通道）。"""
    try:
        from app.services.runtime_params import get_runtime_params

        return get_runtime_params().get_int(SHARE_THRESHOLD_PARAM)
    except Exception:
        return SHARE_THRESHOLD_DEFAULT


def _scheduled_max_submissions() -> int | None:
    """定时作业的单轮提交上限（**运行时参数**，0 = 不限 → None）。

    读不到参数（未初始化 / 规格缺失）时回退到兜底默认值
    :data:`MAX_SUBMISSIONS_PER_RUN`。
    """
    try:
        from app.services.runtime_params import get_runtime_params

        value = get_runtime_params().get_int(MAX_SUBMISSIONS_PARAM)
    except Exception:
        return MAX_SUBMISSIONS_PER_RUN
    return value if value > 0 else None


async def run_group(
    group_id: int,
    result: AggregateSyncResult,
    *,
    max_submissions: int | None = MAX_SUBMISSIONS_PER_RUN,
    wait_for_lock: bool = True,
) -> None:
    """跑一个组的完整流程：**先登记在途 → 再归拢**。

    两条入口（定时作业 / 手动触发）**共用**这一步——副盘是**流动的缓冲区**
    （「数据同步」上传任务持续往缓冲根写新文件），不先登记就永远不会被归拢。

    Args:
        group_id: 聚合组 id。
        result: 结果对象（**就地更新**）。
        max_submissions: 单轮最多提交多少次（None = 不限）。
        wait_for_lock: 锁被占时等待还是跳过。
    """
    group_name = await _group_name(group_id)
    # ① 登记在途（幂等）
    try:
        enq = await enqueue_from_staging_root(group_id)
        if enq["added"]:
            result.details.append(
                f"组「{group_name}」：登记在途 {enq['added']} 个"
                f"（扫描 {enq['scanned']} 个，已存在 {enq['skipped']} 个）"
            )
    except ServiceError as exc:
        # 组没配目标目录 / 没有副盘缓冲根 → 记一笔，不炸整轮
        result.details.append(f"组「{group_name}」：登记在途跳过（{exc}）")
    except Exception as exc:
        logger.warning("组 %s 登记在途失败：%s", group_name, exc)
        result.details.append(f"组「{group_name}」：登记在途失败（{exc}）")

    # ② 归拢
    await _sync_group(
        group_id,
        result,
        max_submissions=max_submissions,
        wait_for_lock=wait_for_lock,
    )


async def _group_name(group_id: int) -> str:
    """取组名（取不到就用 id 兜底）。"""
    async with session_scope() as db:
        row = (
            await db.execute(
                select(AggregateGroup.name).where(AggregateGroup.id == group_id)
            )
        ).scalar_one_or_none()
    return str(row) if row else str(group_id)


async def _sync_group(
    group_id: int,
    result: AggregateSyncResult,
    *,
    max_submissions: int | None = MAX_SUBMISSIONS_PER_RUN,
    wait_for_lock: bool = True,
) -> None:
    """归拢一个聚合组（**持组级锁**——手动触发与 cron 作业互斥）。

    Args:
        group_id: 聚合组 id。
        result: 结果对象（**就地更新**，调用方能实时读进度）。
        max_submissions: 单轮最多提交多少次（``None`` = 不限）。
            - **手动触发不限**（用户盯着，要搬完为止）
            - **定时作业限 20**（无人值守，别一轮跑飞占着事件循环）
        wait_for_lock: 组级锁被别人占着时是**等待**还是**跳过**。
            定时作业 → 跳过（不白等）；手动 → 等待（用户明确要的）。
    """
    lock = await _group_lock(group_id)
    if not wait_for_lock and lock.locked():
        result.details.append("另一轮归拢在跑，本轮跳过（等下一轮）")
        return
    async with lock:
        await _sync_group_locked(
            group_id, result, max_submissions=max_submissions
        )


async def _sync_group_locked(
    group_id: int,
    result: AggregateSyncResult,
    *,
    max_submissions: int | None = MAX_SUBMISSIONS_PER_RUN,
) -> None:
    """归拢一个聚合组（调用方必须已持组级锁）。"""
    async with session_scope() as db:
        group = (
            await db.execute(
                select(AggregateGroup).where(AggregateGroup.id == group_id)
            )
        ).scalar_one_or_none()
        if group is None:
            return
        master = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.aggregate_group_id == group_id,
                        Driver.role == ROLE_MASTER,
                    )
                )
            )
            .scalars()
            .first()
        )
        if master is None:
            logger.info("聚合组 %s 没有主数据源，跳过", group.name)
            return
        # 归拢落点是**组自己的配置**（aggregate_groups.target_dir_id）——
        # 不是主盘的媒体库目录（那是存储源的媒体库配置，与聚合无关）
        if not master.enabled:
            logger.info("聚合组 %s 的主数据源未启用，跳过", group.name)
            result.details.append(f"组「{group.name}」：主数据源未启用，跳过")
            return
        if not group.target_dir_id:
            logger.info("聚合组 %s 未配置归拢目标目录，跳过", group.name)
            result.details.append(
                f"组「{group.name}」：未配置归拢目标目录，跳过（请在「网盘聚合」页配置）"
            )
            return
        master_id = int(master.id)
        master_credential = master.credential
        master_device_type = master.device_type
        root_id = str(group.target_dir_id)
        group_name = group.name

    rows = await _load_processable_rows(group_id)
    if not rows:
        return

    master_driver = build_driver(
        "guangya",
        master_credential,
        master_device_type,
        on_token_refresh=_refresh_callback(master_id),
    )
    index = TargetIndex(master_driver, root_id)
    watcher = TaskWatcher(master_driver, log=logger.info)
    gate = SubmitGate(SUBMIT_INTERVAL_SEC)
    submissions = 0
    #: 副盘驱动按 ``source_driver_id`` 缓存——同一副盘 N 行只建 1 个实例
    #: （每建一次都要走一遍 token 刷新，白费请求）。
    source_drivers: dict[int, Any] = {}

    try:
        for row in rows:
            if max_submissions is not None and submissions >= max_submissions:
                result.details.append(
                    f"组「{group_name}」：本轮已达最大提交数（{max_submissions}），"
                    "其余留到下一轮"
                )
                break
            source = source_drivers.get(row.source_driver_id)
            if source is None:
                source = await _build_source_driver(row)
                source_drivers[row.source_driver_id] = source
            try:
                need_submit = await _sync_one(
                    row,
                    master_driver=master_driver,
                    source=source,
                    index=index,
                    watcher=watcher,
                    gate=gate,
                    result=result,
                    group_name=group_name,
                )
                if need_submit:
                    submissions += 1
            except _QuotaExceeded as exc:
                # **撞墙 = 当天的门关了** → 整组剩余行一次转暂停 + 停手，
                # 不再「一行撞一次」（每次撞墙都是一次白费的服务端请求）。
                # 暂停的行**次日自动续传**（作业把 paused 行捡起来继续）。
                paused = await _pause_group(group_id, str(exc))
                result.paused += paused
                result.details.append(
                    f"组「{group_name}」：{exc}（剩余 {paused} 个文件转暂停，次日自动续传）"
                )
                break
            except Exception as exc:
                await _mark(row.id, STAGING_FAILED, error=f"{type(exc).__name__}: {exc}")
                result.failed += 1
                result.details.append(f"{row.file_name}：失败 {exc}")
                logger.warning("归拢失败 %s：%s", row.file_name, exc)
    finally:
        await watcher.close()


# --------------------------------------------------------------------------
# 单文件归拢
# --------------------------------------------------------------------------


async def _sync_one(
    row: _Row,
    *,
    master_driver: Any,
    source: Any,
    index: TargetIndex,
    watcher: TaskWatcher,
    gate: SubmitGate,
    result: AggregateSyncResult,
    group_name: str,
) -> bool:
    """归拢一个文件。返回是否真的提交了云下载（用于统计本轮提交数）。"""
    await _mark(row.id, STAGING_SYNCING, error=None, bump_attempts=True)

    # ① 目标目录 + 预检
    target_dir_id, existing = await index.resolve(row.target_dir)
    remote_size = existing.get(row.file_name)
    if remote_size is not None:
        if remote_size == row.size_bytes:
            # 主盘已有同名同大小 → 副盘那份是冗余副本
            await _cleanup_source(row, source, result)
            result.skipped += 1
            result.details.append(f"{row.file_name}：主盘已有同名同大小，直接清理副盘")
            return False
        if remote_size > row.size_bytes:
            # 保留大的（主盘那份更大）
            await _cleanup_source(row, source, result)
            result.skipped += 1
            result.details.append(f"{row.file_name}：主盘已有更大版本，直接清理副盘")
            return False
        # 源更大 → 删主盘侧的小文件，然后正常传（保留大的）
        removed = await index.delete_replaced(
            row.target_dir, row.file_name, remote_size
        )
        if not removed:
            raise DriverError(
                "主盘同名文件已变化，未覆盖（不误删）", kind="unknown"
            )

    # ② 认领残留：上一轮已落盘但没改名（中断 / 被取消）
    #
    # 候选是否排除由「**本行是否提交过**」（``row.attempts`` 是本次处理前
    # 的快照值）决定——两个场景对「``download`` 是谁的」判断相反：
    #
    # - **从未提交过**（``attempts == 0``）：目录里的 ``download`` 只可能
    #   是用户自己的文件 → **不认领**（认了就是把用户的文件改名 = 不可逆
    #   的数据损坏）
    # - **提交过**（``attempts > 0``，重试 / 中断恢复）：``download`` 大概率
    #   是我们上一轮的残留 → **不做排除**，认领它（与迁移引擎语义一致）。
    #   残留在目录快照里，排除它 = 永远认领不到 → 重试时重新提交（白扣
    #   额度）+ 落成 ``download(1)`` 还是改不了名 → 死循环 + 垃圾累积。
    #
    # 「提交后改名」对用户原有文件的保护由步骤⑦的排除负责（见下）。
    if row.attempts > 0:
        adopted = await index.rename_landed(
            row.target_dir,
            _landed_candidate_names(),
            row.size_bytes,
            row.file_name,
        )
    else:
        adopted = ""
    if adopted:
        await _cleanup_source(row, source, result)
        result.skipped += 1
        result.details.append(f"{row.file_name}：认领上一轮残留（已落盘未改名）")
        return False

    # ③ **按体积分流**：
    #    - 小文件（≤ 阈值）→ **分享转存**通道（服务端内部复制，**不消耗
    #      云添加额度**）——附件类文件（nfo/jpg/png）占 76% 的文件数却只有
    #      ~0% 体积，走离线下载纯属浪费额度
    #    - 大文件 → 离线下载（云下载）通道（现状）
    threshold = _share_threshold_bytes()
    if threshold > 0 and row.size_bytes <= threshold:
        try:
            transferred = await _transfer_by_share(
                row,
                master_driver=master_driver,
                source=source,
                index=index,
                target_dir_id=target_dir_id,
            )
        except Exception as exc:
            # 分享通道失败 → **回退离线下载**（保底：吃额度但不死）
            logger.warning(
                "分享转存通道失败，回退离线下载：%s（%s）", row.file_name, exc
            )
            result.details.append(
                f"{row.file_name}：分享转存失败，回退离线下载（{exc}）"
            )
        else:
            if transferred:
                # ⚠️ **先写通道、再清理**：反过来会用**过期快照**（row.state
                # 还是处理前的 pending）把刚写好的 done 覆盖回 pending →
                # 行永远回不到完成态 → 下一轮又归拢一遍。
                await _mark(row.id, STAGING_SYNCING, transfer_mode="share_transfer")
                await _cleanup_source(row, source, result)
                result.synced += 1
                result.details.append(
                    f"{row.file_name}：已归拢（分享转存，不消耗云添加额度）"
                )
                return False

    # ④ 取直链（光鸭直链不绑 UA）
    link = await source.get_direct_link(row.remote_id, "")
    if not link.url:
        raise DriverError("副盘返回空直链", kind="not_found")

    # ④ 提交间隔（错开提交，别把光鸭打爆）
    waited = await gate.wait()
    if waited >= 1:
        logger.info("归拢错开提交，等待 %.0fs：%s", waited, row.file_name)

    # ⑤ 提交云下载
    #
    # ⚠️ **额度靠撞墙判定**（没有账本——服务端不提供「剩余次数」查询）：
    # 撞到「次数超限 / 额度 / 配额 / 每日上限」字样 → 抛 _QuotaExceeded →
    # 整组剩余行转暂停，**次日自动续传**。
    # 泛化失败（「云添加失败」code=8）**认不出是不是额度** → 当普通失败，
    # 记 ``failed`` 且**不自动重试**（失败也扣额度，自动重试就是白扣）。
    try:
        task_id = await index.create_offline_task(link.url, target_dir_id)
    except DriverError as exc:
        if _is_quota_error(str(exc)):
            raise _QuotaExceeded(f"云添加额度耗尽：{exc}") from exc
        raise
    await index.note_submitted(row.target_dir, row.file_name, row.size_bytes)
    await _mark(
        row.id,
        STAGING_SYNCING,
        task_id=task_id,
        error=None,
        transfer_mode="cloud_download",
    )

    # ⑥ 等任务结束（从活跃列表消失）——**有上限**：光鸭会把任务卡在活跃
    #    列表不结束（实测），无超时 = run 永不结束 = 其它组全部饿死。
    try:
        await asyncio.wait_for(
            watcher.wait(task_id, path=row.file_name, size=row.size_bytes),
            timeout=TASK_MAX_WAIT_SEC,
        )
    except TimeoutError as exc:
        raise DriverError(
            f"云下载超过 {TASK_MAX_WAIT_SEC / 3600:.0f} 小时仍未结束"
            "（疑似光鸭卡单），已放弃等待——请到光鸭确认后重试",
            kind="unknown",
        ) from exc

    # ⑦ 改名（落盘名是 URL 末段 = download）——**排除提交前就存在的
    #    同名文件**：用户自己有个 ``download`` 时，服务端会把我们的落地
    #    文件改成 ``download(1)``，排除后恰好只改我们那份。
    landed_names = _landed_candidates_for_submit(existing, link.url)
    renamed = await index.rename_landed(
        row.target_dir,
        landed_names,
        row.size_bytes,
        row.file_name,
        # 用户原有的同名文件**跳过**（但服务端自动改名形态 download(N) 照改）
        exclude_names=set(existing),
    )
    if renamed:
        logger.info("归拢改名：%s → %s", renamed, row.file_name)

    # ⑧ 校验：主盘目录里是否真的出现同名同大小文件
    for attempt in range(VERIFY_RETRIES):
        files = await index.refresh(row.target_dir)
        if files.get(row.file_name) == row.size_bytes:
            await _cleanup_source(row, source, result)
            result.synced += 1
            result.details.append(f"{row.file_name}：已归拢")
            return True
        if attempt + 1 < VERIFY_RETRIES:
            await asyncio.sleep(VERIFY_DELAY_SEC)

    reason = await index.failure_reason(task_id)
    if _is_quota_error(reason):
        raise _QuotaExceeded(f"云添加额度耗尽：{reason}")
    raise DriverError(
        reason or "云下载已结束，但主盘目录未出现同名同大小文件", kind="unknown"
    )


async def _transfer_by_share(
    row: _Row,
    *,
    master_driver: Any,
    source: Any,
    index: TargetIndex,
    target_dir_id: str,
) -> bool:
    """用「分享 → 转存」把小文件搬到主盘（**不消耗云添加额度**）。

    链路（2026-11-16 生产实测）::

        源账号 share_create → 取 access_token → 目标账号 share_restore
        → 轮询目标目录直到出现同名同大小文件

    ⚠️ **要点**：

    - 公开接口只认 **URL 形态**的分享 id（``{shareId}_{账号分享key}``），
      裸 shareId → code=112「参数错误」
    - 提取码**必需**（``needCode: true``），否则 209「提取码错误」
    - 转存落地的文件**直接就是原名**（不像云下载要落成 ``download`` 再改名）
    - 转存是**异步任务**且没有可靠的完成查询接口 → 靠**列目录轮询**判定
    - 转存的**配额体系与云添加不同** → 不消耗云添加额度

    Returns:
        True = 已落地（同名同大小）；False = 尚未出现（调用方据此回退）。
    """
    share = await source.create_share([row.remote_id], title=row.file_name)
    try:
        token = await source.share_access_token(share.url_id, share.code)
        await master_driver.restore_share(token, [row.remote_id], target_dir_id)

        deadline = time.monotonic() + SHARE_TRANSFER_WAIT_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(SHARE_TRANSFER_POLL_SEC)
            files = await index.refresh(row.target_dir)
            hit = files.get(row.file_name)
            if hit == row.size_bytes:
                return True
        # 超时：**再列一次**——转存可能刚好落地（只是慢）。确认确实没有，
        # 才让调用方回退离线下载（否则主盘会出现两个同名文件）。
        files = await index.refresh(row.target_dir)
        return files.get(row.file_name) == row.size_bytes
    finally:
        # 分享用完即删（别在账号里堆分享）
        try:
            await source.delete_share([share.share_id])
        except Exception as exc:  # 清理失败不影响归拢结果
            logger.warning("删除分享失败 %s：%s", share.share_id, exc)


async def _cleanup_source(
    row: _Row, source: Any, result: AggregateSyncResult
) -> None:
    """清理副盘副本（**只在主盘确认落盘同名同大小之后**）。

    - 走 ``fs_delete`` → **进回收站**，不是永久删除（可恢复）
    - **记审计日志**——删数据不可逆，必须能回答「删了什么」
    - 单个失败不中断其余条目（只记警告，状态留 ``verified`` 等下轮重试）
    """
    try:
        await source.delete_file(row.remote_id)
    except Exception as exc:
        logger.warning("清理副盘副本失败 %s：%s", row.file_name, exc)
        await _mark(row.id, STAGING_VERIFIED, error=f"清理副盘副本失败：{exc}")
        return
    await _mark(row.id, STAGING_DONE, error=None, cleaned=True)
    result.cleaned += 1
    logger.warning(
        "聚合归拢已清理副盘副本：删除 %s（%d 字节）", row.file_name, row.size_bytes
    )


# --------------------------------------------------------------------------
# 台账读写
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    """台账行的**脱离 ORM 的快照**（会话关闭后仍可安全读取）。"""

    id: int
    group_id: int
    source_driver_id: int
    target_driver_id: int
    remote_id: str
    file_name: str
    source_dir: str
    target_dir: str
    size_bytes: int
    state: str
    attempts: int
    last_error: str | None
    cloud_task_id: str | None


async def _load_processable_rows(group_id: int) -> list[_Row]:
    """取本轮要处理的行（``pending`` / ``paused`` / ``failed`` / ``syncing``）。

    - ``paused``（额度耗尽，**文案明确**）→ 次日额度窗口打开后自动续传
    - ``failed``（泛化失败，认不出是不是额度）→ **不自动重试**，只由人工点
      「重试」——失败也扣额度，自动重试就是白扣
    - ``syncing`` → 先看光鸭活跃任务：**还在跑就跳过**（别重复提交），
      已消失则重置为 ``pending``（残留认领会接住已落盘的情况）；
      **查询失败本轮整批跳过**（分不清在不在跑，保守不碰）
    """
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(SecondaryStaging)
                    .where(
                        SecondaryStaging.group_id == group_id,
                        SecondaryStaging.state.notin_((STAGING_DONE,)),
                    )
                    .order_by(SecondaryStaging.id)
                )
            )
            .scalars()
            .all()
        )
        detached = [
            _Row(
                id=int(r.id),
                group_id=int(r.group_id),
                source_driver_id=int(r.source_driver_id),
                target_driver_id=int(r.target_driver_id),
                remote_id=str(r.remote_id),
                file_name=str(r.file_name),
                source_dir=str(r.source_dir),
                target_dir=str(r.target_dir),
                size_bytes=int(r.size_bytes),
                state=str(r.state),
                attempts=int(r.attempts),
                last_error=r.last_error,
                cloud_task_id=r.cloud_task_id,
            )
            for r in rows
        ]

    active_ids = await _active_task_ids(detached)
    result: list[_Row] = []
    for row in detached:
        if row.state == STAGING_SYNCING:
            if active_ids is None:
                # 查询失败 → **分不清任务还在不在跑** → 保守跳过。
                # 若当「已消失」处理会把它重置为 pending → 重新提交 →
                # 同一文件双任务（白扣额度 + 主盘留 download(1) 垃圾）。
                # 下轮查询恢复后自然接上。128 容器 DNS 偶发故障是已知问题。
                continue
            if row.cloud_task_id and row.cloud_task_id in active_ids:
                continue  # 云下载还在跑 → 本轮不碰
            # 任务已消失（可能已落盘）→ 重置为待归拢，残留认领会接住
            await _mark(row.id, STAGING_PENDING, error=None, task_id="")
            row.state = STAGING_PENDING
        if row.state == STAGING_FAILED:
            # ⚠️ **失败不自动重试**（2026-11-15 决策）——泛化失败认不出是不是
            # 额度问题，而**失败也扣额度** → 自动重试就是白扣。只由人工点
            # 「重试」（:func:`app.services.aggregate_staging.retry_staging`）。
            continue
        result.append(row)
    return result


async def _active_task_ids(rows: list[_Row]) -> set[str] | None:
    """查光鸭在途任务 id 集合。

    Returns:
        任务 id 集合；``None`` = **查询失败**——调用方必须跳过 ``syncing``
        行（分不清「还在跑」与「已结束」，保守不碰）。绝不能返回空集冒充
        「都不在跑」：那会把在途任务重置为待归拢 → 重复提交（白扣额度）。
        128 容器 DNS 偶发故障（AGENTS.md 记录）是已知触发源。
    """
    ids = {r.cloud_task_id for r in rows if r.cloud_task_id}
    if not ids:
        return set()
    async with session_scope() as db:
        driver_row = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.id == rows[0].target_driver_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if driver_row is None:
            return None
        credential = driver_row.credential
        device_type = driver_row.device_type
        driver_id = int(driver_row.id)

    driver = build_driver(
        "guangya",
        credential,
        device_type,
        on_token_refresh=_refresh_callback(driver_id),
    )
    try:
        items = await driver.list_active_offline_tasks()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("读取在途任务失败（本轮跳过 syncing 行，保守不碰）：%s", exc)
        return None
    return {str(item.get("taskId") or "") for item in items}


async def _pause_group(group_id: int, reason: str) -> int:
    """把组内所有待归拢行转 ``paused``（撞上额度墙）。返回转成功的条数。

    含 ``syncing``——撞墙时**当前行**正处在 syncing（提交被拒），它也得
    一起暂停，否则下次作业会把它当「在途任务已消失」重新捡起来。
    """
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(SecondaryStaging).where(
                        SecondaryStaging.group_id == group_id,
                        SecondaryStaging.state.in_((STAGING_PENDING, STAGING_SYNCING)),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.state = STAGING_PAUSED
            row.last_error = reason
            row.updated_at = bj_now()
        await db.commit()
        return len(rows)


async def _mark(
    staging_id: int,
    state: str,
    *,
    error: str | None = None,
    task_id: str | None = None,
    cleaned: bool = False,
    bump_attempts: bool = False,
    transfer_mode: str | None = None,
) -> None:
    """更新台账行状态（**短事务**，不跨网络 IO）。"""
    async with session_scope() as db:
        row = (
            await db.execute(
                select(SecondaryStaging).where(SecondaryStaging.id == staging_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.state = state
        row.last_error = error
        if task_id is not None:
            row.cloud_task_id = task_id or None
        if bump_attempts:
            row.attempts = int(row.attempts) + 1
        if transfer_mode is not None:
            row.transfer_mode = transfer_mode
        if state in (STAGING_VERIFIED, STAGING_DONE):
            # done = 已归拢且副盘副本已清理——「归拢完成时间」对它同样有意义
            row.synced_at = bj_now()
        if cleaned:
            row.cleaned_at = bj_now()
        row.updated_at = bj_now()
        await db.commit()


async def _build_source_driver(row: _Row) -> Any:
    """构造副盘驱动（光鸭）。"""
    async with session_scope() as db:
        driver_row = (
            (
                await db.execute(
                    select(Driver).where(Driver.id == row.source_driver_id)
                )
            )
            .scalars()
            .first()
        )
        if driver_row is None:
            raise ServiceError("副数据源不存在", status=404)
        if driver_row.role != ROLE_SECONDARY:
            raise ServiceError(
                f"存储源「{driver_row.name}」已不是副数据源", status=400
            )
        credential = driver_row.credential
        device_type = driver_row.device_type
        driver_id = int(driver_row.id)
    return build_driver(
        "guangya",
        credential,
        device_type,
        on_token_refresh=_refresh_callback(driver_id),
    )


# --------------------------------------------------------------------------
# 手动触发（API）
# --------------------------------------------------------------------------


async def sync_group_now(
    group_id: int, result: AggregateSyncResult | None = None
) -> AggregateSyncResult:
    """手动跑一轮某个组的归拢（API 触发，**持组级锁**）。

    Args:
        group_id: 聚合组 id。
        result: 外部传入的结果对象——**就地更新**，调用方可实时读到进度
            （后台 job 靠它把计数透给面板）。
    """
    async with session_scope() as db:
        exists = (
            await db.execute(
                select(AggregateGroup.id).where(AggregateGroup.id == group_id)
            )
        ).scalar_one_or_none()
    if exists is None:
        raise ServiceError(f"聚合组不存在：{group_id}", status=404)
    result = result if result is not None else AggregateSyncResult(groups=1)
    lock = await _group_lock(group_id)
    if lock.locked():
        result.details.append("另一轮归拢在跑，本轮等它结束后继续")
    # **手动触发：全量归拢，不限单轮次数**（用户盯着，要搬到台账清空；
    # 只有服务端额度墙才会停——那是硬约束，无视不了）
    await run_group(
        group_id, result, max_submissions=None, wait_for_lock=True
    )
    return result
