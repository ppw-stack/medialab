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

from sqlalchemy import func, select, update

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
from app.services.driver_service import guangya_refresh_callback
from app.services.errors import ServiceError
from app.services.migration_pipeline import (
    GUANGYA_LANDED_NAME,
    VERIFY_DELAY_SEC,
    VERIFY_RETRIES,
    SubmitGate,
    TargetIndex,
    TaskWatcher,
    is_quota_error,
    landed_name_from_url,
)

logger = get_logger(__name__)

__all__ = [
    "AggregateSyncResult",
    "run_aggregate_sync",
    "run_group",
    "sync_group_now",
]

#: 光鸭源的提交间隔（秒）——**1s**（2026-11-16 从 5s 下调）。
#:
#: 额度是**按天**的（撞墙即停、当日不恢复），提交间隔快慢**不影响额度消耗**
#: ——它只影响单轮时长。光鸭源没有下载频控（那种才需要分钟级间隔），
#: 限速由全局限速器（0.2s/请求）兜底 → 1s 足够礼貌。
SUBMIT_INTERVAL_SEC = 1.0

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

#: 运行时参数 key（传输通道模式）。
TRANSFER_MODE_PARAM = "aggregate_transfer_mode"

#: 传输模式：**按体积分流**（旧行为）——小文件走转存，其余走云下载。
TRANSFER_MODE_SIZE_SPLIT = 0

#: 传输模式：**转存优先**（新行为）——所有文件都走「分享转存」。
#:
#: 为什么要有这个模式（2026-11-19 实测）：
#:
#: - **副盘账号非会员时取不到大文件直链**——服务端返回「网页端非会员下载
#:   文件大小超过限制，请开通会员或使用客户端下载」（code=254）。实测
#:   **换客户端身份也绕不开**：dt=1/2/3、客户端 UA、全套 x-* 客户端头，
#:   6 种组合全部同样被拒 → 这条路对非会员是死的
#: - **分享转存不受影响**——服务端内部复制，**不取直链**。实测 **7.74 GB
#:   单文件 11 秒落地**（同名同大小），且**不消耗云添加额度**
#:
#: 故非会员副盘的大文件**只能**走转存。转存失败仍回退云下载（保底）。
TRANSFER_MODE_SHARE_FIRST = 1

#: 传输模式的兜底默认值（**转存优先**）。
TRANSFER_MODE_DEFAULT = TRANSFER_MODE_SHARE_FIRST

#: 分享转存通道：等文件出现在目标目录的上限（秒）。
#:
#: 转存是**异步任务**（返回 taskId），但**没有可靠的完成查询接口**
#: （``get_task_status`` 是上传任务的接口，查云下载任务返回 145）→ 只能
#: **列目录轮询**（与「校验落盘」同一条判据）。
SHARE_TRANSFER_WAIT_SEC = 600.0

#: 分享转存通道：列目录轮询间隔（秒）
SHARE_TRANSFER_POLL_SEC = 3.0

#: 转存报「未选择转存文件」时的等待（秒）——**分享内容还没索引完**。
#:
#: 2026-11-19 生产实测：刚建好的分享立刻转存报「未选择转存文件」，
#: 同样的代码 2 分钟后成功（7.74 GB 的文件一次就落地）。故这是
#: **竞态**，不是「这个文件不能转存」。
SHARE_SETTLE_SEC = 2.5

#: 「分享内容未就绪」的判定文案（服务端返回的 msg 里出现它）。
_SHARE_NOT_READY_HINT = "未选择转存文件"


class _ShareContentRejected(DriverError):
    """分享内容为空——**服务端逐文件审核拒绝**（2026-11-19 实测）。

    光鸭对每个文件**单独**判定能不能分享：被拒的文件**不进分享**，
    转存必然报「未选择转存文件」。生产实测 58 个文件里 44 个如此。

    这个错**重试没有意义**（不是竞态）——只能换通道（云下载），
    而云下载要先取直链（非会员副盘取不到，见 :data:`TRANSFER_MODE_SHARE_FIRST`）。
    """

    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            f"分享内容为空（{len(missing)} 个文件未通过分享审核）："
            f"{', '.join(missing[:3])}"
            + ("…" if len(missing) > 3 else ""),
            kind="not_found",
        )
        #: 未进分享的文件 id
        self.missing = missing

#: 分享转存通道：**一个分享最多打包多少个文件**。
#:
#: 分享可以带一个文件列表（`share_create(file_ids)`）、一次转存整批 →
#: N 个附件从「N 次（4 请求 + 轮询）」变成「1 次分享 + 1 次转存」。
#:
#: ⚠️ **光鸭有「分享文件数量」限制：实测 20 个 ✅、25 个 ❌**
#: （「分享目录文件数量超过限制」，2026-11-16 二分探明：10/20 成功、
#: 25/30/35/40/50 全失败）。超限的批次**建分享直接失败** → 触发逐个
#: 回退云下载（白扣额度）——所以这个数**不能拍脑袋调大**。
SHARE_BATCH_SIZE = 20

#: 分享转存通道：**一个分享最多打包多少字节**（体积上限）。
#:
#: 数量上限管不住体积——20 个 5 GB 的剧集 = 100 GB 的一个分享，服务端很可能
#: 拒收（数量与体积是两个维度）。实测**单文件 7.74 GB 的分享可建可转存**
#: （2026-11-19），故把单批体积压在 10 GB 内：大文件自动单独成批，
#: 小文件照旧打包（附件才是吞吐大头）。
#:
#: 超限的风险是「整批失败 → 逐个回退云下载」——非会员副盘回退也搬不动，
#: 故宁可拆小。
SHARE_BATCH_MAX_BYTES = 10 * 1024 * 1024 * 1024

#: ⚠️ **失败的文件不自动重试**（2026-11-15 决策）
#:
#: 光鸭撞额度时**可能只回泛化的「云添加失败」**（无「次数超限」字样），
#: 认不出是额度问题；而**失败也扣额度** → 自动重试就是白扣，且永远等不到
#: 「自动续传」。故泛化失败一律记 ``failed``，**只由人工点「重试」**。
#:
#: 注意：**暂停**（``paused``，文案明确是额度耗尽）**仍会自动续传**——
#: 次日额度窗口打开，作业把 ``paused`` 行捡起来继续。

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


def _landed_candidate_names(url: str = "") -> tuple[str, ...]:
    """落盘名候选**全集**（不做任何排除）。

    云下载落盘名取自 URL 末段（光鸭直链 = ``download``），撞名时服务端会
    自动改成 ``download(1)``（:func:`app.services.migration_pipeline.TargetIndex.
    rename_landed` 已支持该形态）。
    """
    names = {GUANGYA_LANDED_NAME}
    derived = landed_name_from_url(url)
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
    """小文件走「分享转存」的体积上限（**运行时参数**，0 = 关闭该通道）。

    ⚠️ 只在**按体积分流**模式下生效（见 :func:`_transfer_mode`）。
    """
    try:
        from app.services.runtime_params import get_runtime_params

        return get_runtime_params().get_int(SHARE_THRESHOLD_PARAM)
    except Exception:
        return SHARE_THRESHOLD_DEFAULT


def _transfer_mode() -> int:
    """传输通道模式（**运行时参数**）。

    - :data:`TRANSFER_MODE_SHARE_FIRST` —— **转存优先**：所有文件都走
      「分享转存」（不取直链、不耗云添加额度），失败才回退云下载。
      **非会员副盘必须用它**（取不到大文件直链）。
    - :data:`TRANSFER_MODE_SIZE_SPLIT` —— 按体积分流（旧行为）。
    """
    try:
        from app.services.runtime_params import get_runtime_params

        value = get_runtime_params().get_int(TRANSFER_MODE_PARAM)
    except Exception:
        return TRANSFER_MODE_DEFAULT
    return TRANSFER_MODE_SHARE_FIRST if value >= 1 else TRANSFER_MODE_SIZE_SPLIT


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
    include_failed: bool = False,
) -> None:
    """跑一个组的完整流程：**先登记在途 → 再归拢**。

    两条入口（定时作业 / 手动触发）**共用**这一步——副盘是**流动的缓冲区**
    （「数据同步」上传任务持续往缓冲根写新文件），不先登记就永远不会被归拢。

    Args:
        group_id: 聚合组 id。
        result: 结果对象（**就地更新**）。
        max_submissions: 单轮最多提交多少次（None = 不限）。
        wait_for_lock: 锁被占时等待还是跳过。
        include_failed: 是否把 ``failed`` 行也纳入本轮处理。**默认 False**——
            定时作业不自动重试失败（泛化失败认不出是不是额度问题，而失败也扣
            额度，自动重试就是白扣）；**手动触发传 True**——用户盯着、明确要
            重试（人工意志，不是自动重试）。
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
        include_failed=include_failed,
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
    include_failed: bool = False,
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
        include_failed: ``failed`` 行是否纳入本轮（手动 = True，定时 = False）。
    """
    lock = await _group_lock(group_id)
    if not wait_for_lock and lock.locked():
        result.details.append("另一轮归拢在跑，本轮跳过（等下一轮）")
        return
    async with lock:
        await _sync_group_locked(
            group_id,
            result,
            max_submissions=max_submissions,
            include_failed=include_failed,
        )


async def _sync_group_locked(
    group_id: int,
    result: AggregateSyncResult,
    *,
    max_submissions: int | None = MAX_SUBMISSIONS_PER_RUN,
    include_failed: bool = False,
) -> None:
    """归拢一个聚合组（调用方必须已持组级锁）。

    Args:
        group_id: 聚合组 id。
        result: 结果对象（**就地更新**，调用方能实时读进度）。
        max_submissions: 单轮最多提交多少次（``None`` = 不限）。
        include_failed: ``failed`` 行是否纳入本轮（手动 = True，定时 = False）。
    """
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
        root_id = str(group.target_dir_id)
        group_name = group.name

    rows = await _load_processable_rows(
        group_id, include_failed=include_failed
    )
    if not rows:
        return

    master_driver = build_driver(
        "guangya",
        master_credential,
        on_token_refresh=guangya_refresh_callback(master_id),
    )
    index = TargetIndex(master_driver, root_id)
    # ⚠️ 轮询间隔显式 **2s**（默认 5s → 完成判定要 2 次缺席 ≈ 10s/文件，
    #    是串行归拢最大的固定开销）。限速由全局限速器兜底，2s 很安全。
    watcher = TaskWatcher(master_driver, log=logger.info, poll_interval=2.0)
    gate = SubmitGate(SUBMIT_INTERVAL_SEC)
    #: 副盘驱动按 ``source_driver_id`` 缓存——同一副盘 N 行只建 1 个实例
    #: （每建一次都要走一遍 token 刷新，白费请求）。
    source_drivers: dict[int, Any] = {}

    # ① **逐行预检**（两条通道共用）：主盘已有同名文件的处理 + 分流
    threshold = _share_threshold_bytes()
    mode = _transfer_mode()
    transfer_rows: list[_Row] = []  # 可批量转存（小文件、无同名冲突）
    cloud_rows: list[_Row] = []     # 走云下载（大文件 / replace / 阈值关闭）
    for row in rows:
        source = source_drivers.get(row.source_driver_id)
        if source is None:
            source = await _build_source_driver(row)
            source_drivers[row.source_driver_id] = source
        try:
            action, dir_id, _existing = await _precheck_target(
                row, index=index, source=source, result=result
            )
            row.target_dir_id = dir_id
        except Exception as exc:
            # 预检失败（网络 / 同名文件已变化）→ 保守走云下载（现有判定兜底）
            logger.warning("预检失败 %s：%s", row.file_name, exc)
            cloud_rows.append(row)
            continue
        if action == "skip":
            continue  # 已清理副盘副本并标 done
        if action == "replace":
            # 主盘已有**更小的**同名文件 → 删旧重传。云下载通道才能删旧
            # 再传（转存落地同名文件的覆盖语义未验证，保守走云下载）
            cloud_rows.append(row)
        elif mode == TRANSFER_MODE_SHARE_FIRST:
            # **转存优先**：不取直链、不耗云添加额度——非会员副盘取不到
            # 大文件直链，这是唯一能搬的路（2026-11-19 实测 7.74 GB 11 秒
            # 落地）。转存失败仍回退云下载（见下）
            transfer_rows.append(row)
        elif threshold <= 0 or row.size_bytes > threshold:
            cloud_rows.append(row)
        else:
            transfer_rows.append(row)

    # ② **小文件批量打包转存**（不消耗额度）——附件占文件数的大头，
    #    一个分享打包 N 个附件、一次转存整批，是吞吐的最大杠杆。
    #    **与云下载通道并行**（见 ③）。
    share_task: asyncio.Task[list[_Row]] | None = None
    if transfer_rows:
        result.details.append(
            f"组「{group_name}」：{len(transfer_rows)} 个小文件走分享转存（批量），"
            f"{len(cloud_rows)} 个走云下载（两条通道并行）"
        )
        share_task = asyncio.create_task(
            _batch_share_transfer(
                transfer_rows,
                master_driver=master_driver,
                index=index,
                result=result,
                source_drivers=source_drivers,
            )
        )

    # ③ **两条通道并行**：
    #    - 云下载通道（大文件 / replace / 回退）：恒串行（落盘名撞车 + 额度）
    #    - 分享转存通道（小文件）：批量 + 多批并行（不占额度、不撞名）
    #    两条通道的瓶颈不同（云下载=额度与提交间隔；转存=服务端落地延迟）→
    #    并行时互不抢资源，附件批次在等落地的时间正好让视频跑起来。
    #
    #    额度墙只暂停**云下载通道**的行——转存通道不受额度影响，不该被连坐。
    budget = _CloudBudget(max_submissions)
    cloud_task = asyncio.create_task(
        _run_cloud_rows(
            cloud_rows,
            master_driver=master_driver,
            index=index,
            watcher=watcher,
            gate=gate,
            result=result,
            group_name=group_name,
            budget=budget,
            source_drivers=source_drivers,
        )
    )
    try:
        cloud_remaining, wall_hit = await cloud_task
        # 转存通道收尾（可能早已结束）
        fallback: list[_Row] = []
        if share_task is not None:
            fallback = await share_task
        if fallback:
            if wall_hit:
                # 额度已耗尽 → 回退行直接暂停（次日续传），不再白撞
                paused = await _pause_rows(fallback, "云添加额度耗尽")
                result.paused += paused
                result.details.append(
                    f"组「{group_name}」：{len(fallback)} 个小文件转存未落地，"
                    f"且云添加额度已耗尽 → {paused} 个转暂停（次日续传）"
                )
            else:
                # 额度还有 → 回退行再走一轮云下载
                result.details.append(
                    f"组「{group_name}」：{len(fallback)} 个小文件转存未落地，回退云下载"
                )
                more, wall_hit2 = await _run_cloud_rows(
                    fallback,
                    master_driver=master_driver,
                    index=index,
                    watcher=watcher,
                    gate=gate,
                    result=result,
                    group_name=group_name,
                    budget=budget,
                    source_drivers=source_drivers,
                )
                cloud_remaining = more + cloud_remaining
                wall_hit = wall_hit or wall_hit2
        if cloud_remaining and not wall_hit:
            result.details.append(
                f"组「{group_name}」：{len(cloud_remaining)} 个文件留到下一轮"
            )
    finally:
        await watcher.close()


# --------------------------------------------------------------------------
# 单文件归拢
# --------------------------------------------------------------------------


async def _precheck_target(
    row: _Row,
    *,
    index: TargetIndex,
    source: Any,
    result: AggregateSyncResult,
) -> tuple[str, str, dict[str, int]]:
    """目标目录预检（**两条通道共用**）：主盘已有同名文件的处理。

    **为什么必须共用**：批量转存曾漏掉这一步 → 转存撞上「主盘已有同名文件」
    → 光鸭失败或自动改名 ``xxx(1)`` → 轮询条件「原名+副盘大小」永远不命中
    → **每批烧满超时**（600s×N），啥也搬不动（2026-11-16 生产实测）。

    Returns:
        ``(action, target_dir_id, existing)``：

        - ``action="skip"`` —— 主盘已有同名同大小 / 更大版本 → 副盘是冗余
          副本，**已清理并标 done**（0 额度），调用方跳过该行
        - ``action="replace"`` —— 主盘同名更小，**已删主盘侧旧文件** →
          走云下载重传（转存遇同名会失败/改名，不能走转存）
        - ``action="transfer"`` —— 无同名 → 可走任意通道（批量转存优先）
        - ``existing`` —— 预检时的目录快照（改名时排除用户原有文件用）
    """
    target_dir_id, existing = await index.resolve(row.target_dir)
    remote_size = existing.get(row.file_name)
    if remote_size is None:
        return "transfer", target_dir_id, existing
    if remote_size == row.size_bytes:
        # 主盘已有同名同大小 → 副盘那份是冗余副本（0 额度）
        await _cleanup_source(row, source, result)
        result.skipped += 1
        result.details.append(f"{row.file_name}：主盘已有同名同大小，直接清理副盘")
        return "skip", target_dir_id, existing
    if remote_size > row.size_bytes:
        # 保留大的（主盘那份更大）→ 副盘冗余（0 额度）
        await _cleanup_source(row, source, result)
        result.skipped += 1
        result.details.append(f"{row.file_name}：主盘已有更大版本，直接清理副盘")
        return "skip", target_dir_id, existing
    # 源更大 → 删主盘侧的小文件，然后重传（保留大的）
    # ⚠️ 只能走**云下载**：转存遇目标同名会失败/被服务端改名
    removed = await index.delete_replaced(row.target_dir, row.file_name, remote_size)
    if not removed:
        raise DriverError("主盘同名文件已变化，未覆盖（不误删）", kind="unknown")
    return "replace", target_dir_id, existing


class _CloudBudget:
    """云下载通道的**提交预算**（单轮共享，两条通道并行时也一起算）。

    上限来自运行时参数（``aggregate_max_submissions``；``None`` = 不限）。
    分享转存通道**不占**这个预算（它不吃云添加额度）。
    """

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit


async def _run_cloud_rows(
    rows: list[_Row],
    *,
    master_driver: Any,
    index: TargetIndex,
    watcher: TaskWatcher,
    gate: SubmitGate,
    result: AggregateSyncResult,
    group_name: str,
    budget: _CloudBudget,
    source_drivers: dict[int, Any],
) -> tuple[list[_Row], bool]:
    """云下载通道：恒串行搬一批行（大文件 / replace / 转存回退）。

    Returns:
        ``(未处理的行, 是否撞了额度墙)``。
    """
    for idx, row in enumerate(rows):
        if budget.exhausted:
            result.details.append(
                f"组「{group_name}」：本轮已达最大提交数（{budget.limit}），"
                "其余留到下一轮"
            )
            return rows[idx:], False
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
                budget.used += 1
        except _QuotaExceeded as exc:
            # **撞墙 = 当天的门关了** → 剩余行一次转暂停 + 停手，
            # 不再「一行撞一次」（每次撞墙都是一次白费的服务端请求）。
            # ⚠️ **只暂停云下载通道的行**——分享转存通道不受额度影响，
            # 早前整组连坐暂停会把附件也拖到次日。
            # 暂停的行**次日自动续传**（作业把 paused 行捡起来继续）。
            paused = await _pause_rows(rows[idx:], str(exc))
            result.paused += paused
            result.details.append(
                f"组「{group_name}」：{exc}（剩余 {paused} 个文件转暂停，次日自动续传）"
            )
            return [], True
        except Exception as exc:
            await _mark(row.id, STAGING_FAILED, error=f"{type(exc).__name__}: {exc}")
            result.failed += 1
            result.details.append(f"{row.file_name}：失败 {exc}")
            logger.warning("归拢失败 %s：%s", row.file_name, exc)
    return [], False


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

    # ① 目标目录预检（**与批量转存共用**）：主盘已有同名文件的处理
    action, target_dir_id, existing = await _precheck_target(
        row, index=index, source=source, result=result
    )
    if action == "skip":
        return False

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
                await _mark_share_transferred(row, source, result)
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
        if is_quota_error(str(exc)):
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
    if is_quota_error(reason):
        raise _QuotaExceeded(f"云添加额度耗尽：{reason}")
    raise DriverError(
        reason or "云下载已结束，但主盘目录未出现同名同大小文件", kind="unknown"
    )


async def _restore_share_with_retry(
    source: Any,
    master_driver: Any,
    file_ids: list[str],
    parent_id: str,
    *,
    title: str,
) -> tuple[Any, str, list[str]]:
    """建分享 → 取 token → 转存，**内容未就绪时重试**。

    ⚠️ 光鸭刚建好的分享**内容还没索引完** → 转存报「未选择转存文件」
    （2026-11-19 生产实测：第一个分享失败，2 分钟后同代码成功落地
    7.74 GB）。这是**竞态**，不是「这个文件不能转存」。

    重试策略：同分享等 :data:`SHARE_SETTLE_SEC` 再试 → 仍失败则**重建
    分享**再试一次（分享本身可能有问题）。

    ⚠️ **列分享内容失败 ≠ 审核拒绝**：列表接口抖动时按「内容齐全」继续
    （**不做审核过滤**），让转存接口自己报错（那个错有信息量）。
    ⚠️ **部分文件缺失先等再判**：分享内容可能只索引了一半 → 等
    :data:`SHARE_SETTLE_SEC` 重列一次，才判「审核拒绝」——非会员副盘取不到
    直链，被误判就等于这个文件彻底搬不动了。

    Returns:
        ``(share, task_id, dropped_ids)``——

        - **分享不要提前删**：转存是异步任务，删了可能中断服务端复制
          （由调用方在轮询结束后删）
        - ``dropped_ids`` = **没进分享的文件**（服务端逐文件审核拒绝）→
          它们**不在**本次转存里，调用方要把它们交给别的通道（回退云下载）。
          只转存分享里有的文件 → 一个被拒的文件**不会连坐整批**

    Raises:
        Exception: 重试仍失败时抛出（调用方回退云下载）。
    """
    share = None
    try:
        share = await source.create_share(file_ids, title=title)
        token = await source.share_access_token(share.url_id, share.code)
        # ⚠️ **分享内容可能被审核清空**（逐文件判定）——转存必然报「未选择
        # 转存文件」，重试也没用（不是竞态）。先确认文件在分享里，别白撞。
        # （实测 58 个文件里 44 个的分享内容为空 → 白撞就是白撞）
        ids = await _share_content_ids(source, token)
        missing: list[str] = []
        present: list[str] = list(file_ids)
        if ids is not None:
            missing = [fid for fid in file_ids if fid not in ids]
            if missing:
                # ⚠️ **内容可能还没索引完**（真实竞态）→ 等一下再列一次，
                # 别把只是 lag 的文件当成「审核拒绝」永久踢出本批转存
                # （非会员副盘取不到直链 → 被踢出就等于搬不动）
                await asyncio.sleep(SHARE_SETTLE_SEC)
                retry_ids = await _share_content_ids(source, token)
                if retry_ids:
                    ids = retry_ids
                    missing = [fid for fid in file_ids if fid not in ids]
            present = [fid for fid in file_ids if fid in ids]
        # ⚠️ **列表失败（``ids is None``）≠ 审核拒绝** → 不做审核过滤
        # （``present`` 就是全部文件），让转存接口自己报错（那个错有信息量）
        if not present:
            raise _ShareContentRejected(missing)
        if missing:
            # 部分文件没进分享 → **只转存有的那些**，被拒的交给别的通道
            logger.warning(
                "分享内容缺 %d 个文件（审核拒绝），只转存其余 %d 个",
                len(missing),
                len(present),
            )
        try:
            task_id = await master_driver.restore_share(
                token, present, parent_id
            )
            return share, task_id, missing
        except Exception as exc:
            if _SHARE_NOT_READY_HINT not in str(exc):
                raise
            logger.warning(
                "转存时分享内容未就绪（%s），等待 %.1fs 后重试",
                exc,
                SHARE_SETTLE_SEC,
            )
            await asyncio.sleep(SHARE_SETTLE_SEC)
            try:
                task_id = await master_driver.restore_share(
                    token, present, parent_id
                )
                return share, task_id, missing
            except Exception as exc2:
                if _SHARE_NOT_READY_HINT not in str(exc2):
                    raise
                logger.warning("分享内容仍未就绪，重建分享再试一次")
                await asyncio.sleep(SHARE_SETTLE_SEC)
                await source.delete_share([share.share_id])
                share = await source.create_share(present, title=title)
                token = await source.share_access_token(share.url_id, share.code)
                task_id = await master_driver.restore_share(
                    token, present, parent_id
                )
                return share, task_id, missing
    except Exception:
        # 失败路径：分享用完即删（别在账号里堆分享）
        if share is not None:
            try:
                await source.delete_share([share.share_id])
            except Exception as exc:  # 清理失败不影响归拢结果
                logger.warning("删除分享失败 %s：%s", share.share_id, exc)
        raise


async def _share_content_ids(source: Any, token: str) -> set[str] | None:
    """取分享里的文件 id 集合（**接口抖动不该否决整批**）。

    Returns:
        ``set[str]`` = 成功列到的内容 id（**空集** = 分享确实空了）；
        ``None`` = **列表失败**（接口抖动）→ 调用方按「**不做审核过滤**」
        继续（宁可让转存接口自己报错，也不要因为一次列表失败就把整批
        当成「审核拒绝」）。

    ⚠️ **不能拿空集当「列表失败」**：空集在下游等价于「所有文件都缺失」
    → 整批被判「审核拒绝」→ 白白回退云下载（每天仅 3 次的云添加额度）。
    「未知」与「为空」必须用两个不同的返回值区分。
    """
    try:
        ids = await source.share_files(token)
    except Exception as exc:
        logger.warning("列分享内容失败（按「内容齐全」继续）：%s", exc)
        return None
    if not ids:
        # 可能是索引延迟 → 等一下再看一次
        await asyncio.sleep(SHARE_SETTLE_SEC)
        try:
            ids = await source.share_files(token)
        except Exception as exc:
            logger.warning("列分享内容失败（按「内容齐全」继续）：%s", exc)
            return None
    return set(ids)


async def _transfer_by_share(
    row: _Row,
    *,
    master_driver: Any,
    source: Any,
    index: TargetIndex,
    target_dir_id: str,
) -> bool:
    """用「分享 → 转存」把文件搬到主盘（**不消耗云添加额度**）。

    链路（2026-11-16 生产实测，2026-11-19 扩展到大文件）::

        源账号 share_create → 取 access_token → 目标账号 share_restore
        → 轮询目标目录直到出现同名同大小文件

    ⚠️ **要点**：

    - 公开接口只认 **URL 形态**的分享 id（``{shareId}_{账号分享key}``），
      裸 shareId → code=112「参数错误」
    - 提取码**必需**（``needCode: true``），否则 209「提取码错误」
    - 转存落地的文件**直接就是原名**（不像云下载要落成 ``download`` 再改名）
    - 转存是**异步任务**且没有可靠的完成查询接口 → 靠**列目录轮询**判定
    - 转存的**配额体系与云添加不同** → 不消耗云添加额度
    - **不取直链** → 非会员副盘也搬得动（2026-11-19 实测 7.74 GB 单文件
      11 秒落地；非会员取直链被服务端拒且换客户端身份也不行）

    Returns:
        True = 已落地（同名同大小）；False = 尚未出现（调用方据此回退）。
    """
    share, _task_id, dropped = await _restore_share_with_retry(
        source,
        master_driver,
        [row.remote_id],
        target_dir_id,
        title=row.file_name,
    )
    if dropped:
        # 审核拒绝 → 这个文件搬不动（单文件路径没有「同批其它文件」）
        try:
            await source.delete_share([share.share_id])
        except Exception as exc:  # 清理失败不影响归拢结果
            logger.warning("删除分享失败 %s：%s", share.share_id, exc)
        return False
    try:
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


#: 分享转存通道：**同时在途的批次上限**。
#:
#: 分享通道**没有**云下载的那两条串行约束（落盘名撞车 / 额度按次）——
#: 转存落地保持原名、不消耗额度 → 多批可以并行。服务端的转存任务落地
#: 延迟实测 ~10 分钟/批 → 串行时吞吐被延迟卡死（20 个/10 分钟），
#: 3–5 批并行可放大 3–5 倍。
SHARE_MAX_IN_FLIGHT = 3


async def _transfer_one_batch(
    chunk: list[_Row],
    *,
    source: Any,
    master_driver: Any,
    index: TargetIndex,
    result: AggregateSyncResult,
) -> list[_Row]:
    """跑**一个**分享批次：建分享 → 转存 → 轮询 → 清理。

    Returns:
        **未落地**的行（调用方回退到云下载通道）。
    """
    for row in chunk:
        await _mark(
            row.id,
            STAGING_SYNCING,
            error=None,
            bump_attempts=True,
            transfer_mode="share_transfer",
        )
    share: Any = None
    try:
        share, _task_id, dropped = await _restore_share_with_retry(
            source,
            master_driver,
            [r.remote_id for r in chunk],
            chunk[0].target_dir_id,
            title=f"{chunk[0].file_name} 等 {len(chunk)} 个",
        )

        # 轮询：整批出现（同名同大小）或超时
        deadline = time.monotonic() + SHARE_TRANSFER_WAIT_SEC
        # ⚠️ **被审核拒绝的文件不在本次转存里** → 不等它们落地，直接
        # 交给回退通道（别让它们白等满 600 秒）
        dropped_ids = set(dropped)
        pending = {
            r.file_name: r
            for r in chunk
            if r.remote_id not in dropped_ids
        }
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(SHARE_TRANSFER_POLL_SEC)
            files = await index.refresh(chunk[0].target_dir)
            for r in list(pending.values()):
                if files.get(r.file_name) == r.size_bytes:
                    del pending[r.file_name]
        # 超时后再确认一次（转存可能刚好落地）
        files = await index.refresh(chunk[0].target_dir)
        for r in list(pending.values()):
            if files.get(r.file_name) == r.size_bytes:
                del pending[r.file_name]

        # ⚠️ **被拒的文件既没落地、也不在 pending 里** → 必须显式区分，
        # 否则会被当成「已落地」去清理副盘副本（数据没了）
        landed = [
            r
            for r in chunk
            if r.remote_id not in dropped_ids and r.file_name not in pending
        ]
        if landed:
            # **批量清理**副盘副本（一批一次请求；失败逐个兜底）
            try:
                await source.delete_files([r.remote_id for r in landed])
                for r in landed:
                    await _mark(r.id, STAGING_DONE, error=None, cleaned=True)
                result.cleaned += len(landed)
                result.synced += len(landed)
                logger.warning(
                    "聚合归拢已清理副盘副本：%d 个（分享转存批次，%s 等）",
                    len(landed),
                    landed[0].file_name,
                )
            except Exception as exc:
                logger.warning("批量清理副盘副本失败，逐个重试：%s", exc)
                for r in landed:
                    await _cleanup_source(r, source, result)
        not_landed = [
            r
            for r in chunk
            if r.remote_id in dropped_ids or r.file_name in pending
        ]
        if dropped:
            result.details.append(
                f"分享审核拒绝 {len(dropped)} 个文件（未进分享）→ 回退云下载"
            )
        if landed and not_landed:
            result.details.append(
                f"批量转存超时：{len(landed)} 个落地、"
                f"{len(not_landed)} 个未落地 → 未落地的回退云下载"
            )
        return not_landed
    except Exception as exc:
        # ⚠️ **整批失败也要把行交给回退**——曾漏掉（not_landed 只在
        #   try 块里赋值）→ 回退列表为空 → 云下载循环空转。
        logger.warning("分享转存批次失败（%d 个文件）：%s", len(chunk), exc)
        result.details.append(
            f"批量转存失败（{len(chunk)} 个文件）：{exc} → 逐个重试"
        )
        # ⚠️ **逐个重试**：分享审核是**逐文件**的（2026-11-19 实测：58 个
        # 失败行里 14 个能进分享、44 个进不去）——一个被拒的文件会把**整批**
        # 拖下水。逐个重试能把能搬的搬走，只让真正搬不动的回退云下载。
        return await _transfer_rows_one_by_one(
            chunk,
            source=source,
            master_driver=master_driver,
            index=index,
            result=result,
        )
    finally:
        if share is not None:
            try:
                await source.delete_share([share.share_id])
            except Exception as exc:  # 清理失败不影响归拢
                logger.warning("删除分享失败 %s：%s", share.share_id, exc)


async def _mark_share_transferred(
    row: _Row, source: Any, result: AggregateSyncResult
) -> None:
    """分享转存落地后的收尾：写通道 → 清理副盘副本 → 计数。

    ⚠️ **先写通道、再清理**：反过来会用**过期快照**（row.state 还是处理前
    的 pending）把刚写好的 done 覆盖回 pending → 行永远回不到完成态 →
    下一轮又归拢一遍。
    """
    await _mark(row.id, STAGING_SYNCING, transfer_mode="share_transfer")
    await _cleanup_source(row, source, result)
    result.synced += 1
    result.details.append(
        f"{row.file_name}：已归拢（分享转存，不消耗云添加额度）"
    )


async def _transfer_rows_one_by_one(
    chunk: list[_Row],
    *,
    source: Any,
    master_driver: Any,
    index: TargetIndex,
    result: AggregateSyncResult,
) -> list[_Row]:
    """整批失败后**逐个重试**（分享审核是逐文件判定的）。

    一个被审核拒绝的文件会让整批转存失败——逐个重试才能把**能搬的**搬走。

    Returns:
        仍然搬不动的行（调用方回退云下载）。
    """
    still_bad: list[_Row] = []
    for row in chunk:
        try:
            ok = await _transfer_by_share(
                row,
                master_driver=master_driver,
                source=source,
                index=index,
                target_dir_id=row.target_dir_id,
            )
        except Exception as exc:
            logger.warning("逐个转存失败 %s：%s", row.file_name, exc)
            ok = False
        if ok:
            await _mark_share_transferred(row, source, result)
        else:
            still_bad.append(row)
    return still_bad


def _split_share_batches(rows: list[_Row]) -> list[list[_Row]]:
    """把同 (源账号, 目标目录) 的行切成分享批次。

    两个上限都管：**文件数**（光鸭实测 20 个封顶）与**总字节**
    （大文件单独成批——单文件 7.74 GB 的分享实测可用）。
    """
    batches: list[list[_Row]] = []
    current: list[_Row] = []
    current_bytes = 0
    for row in rows:
        size = int(row.size_bytes)
        too_big = (
            current
            and (
                len(current) >= SHARE_BATCH_SIZE
                or current_bytes + size > SHARE_BATCH_MAX_BYTES
            )
        )
        if too_big:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(row)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


async def _batch_share_transfer(
    rows: list[_Row],
    *,
    master_driver: Any,
    index: TargetIndex,
    result: AggregateSyncResult,
    source_drivers: dict[int, Any],
) -> list[_Row]:
    """**批量打包转存**（rows 已过预检：无同名冲突、目标目录已解析）。

    文件可以是任意体积——「转存优先」模式下大文件也走这里（不取直链）。

    分组规则：**(源账号, 目标目录)**——分享必须建在文件自己的账号上，
    转存只能落到一个目录 → 跨目录/跨账号的行各自成批。

    **批次并行**（``SHARE_MAX_IN_FLIGHT``）：分享通道没有云下载的两条串行
    约束（落盘名撞车 / 按次额度）→ 多批可以同时转存，吞吐 ×3–5。

    Returns:
        **未落地**的行（调用方回退到云下载通道）。
    """
    not_landed: list[_Row] = []
    groups: dict[tuple[int, str], list[_Row]] = {}
    for row in rows:
        groups.setdefault((row.source_driver_id, row.target_dir_id), []).append(row)

    # 分组 → 批次（(源账号, 批次行) 列表）
    batches: list[tuple[int, list[_Row]]] = []
    for (source_driver_id, _target_dir), chunk_rows in groups.items():
        batches.extend(
            (source_driver_id, chunk)
            for chunk in _split_share_batches(chunk_rows)
        )

    async def _run(batch: tuple[int, list[_Row]]) -> list[_Row]:
        source_driver_id, chunk = batch
        async with await _semaphore():
            source = source_drivers.get(source_driver_id)
            if source is None:
                source = await _build_source_driver(chunk[0])
                source_drivers[source_driver_id] = source
            return await _transfer_one_batch(
                chunk,
                source=source,
                master_driver=master_driver,
                index=index,
                result=result,
            )

    gathered = await asyncio.gather(
        *(_run(b) for b in batches), return_exceptions=True
    )
    for item in gathered:
        if isinstance(item, BaseException):
            logger.warning("分享转存批次异常：%s", item)
            continue
        not_landed.extend(item)
    return not_landed


#: 批次并发的信号量（进程级，跨组共用）
_SHARE_SEMAPHORE: asyncio.Semaphore | None = None

async def _semaphore() -> asyncio.Semaphore:
    """取批次并发信号量（惰性创建，绑定当前事件循环）。"""
    global _SHARE_SEMAPHORE
    if _SHARE_SEMAPHORE is None:
        _SHARE_SEMAPHORE = asyncio.Semaphore(SHARE_MAX_IN_FLIGHT)
    return _SHARE_SEMAPHORE


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
    #: 预检时解析出的**目标目录 id**（主盘侧；批量转存直接用，不再二次解析）
    target_dir_id: str = ""


async def _load_processable_rows(
    group_id: int, *, include_failed: bool = False
) -> list[_Row]:
    """取本轮要处理的行（``pending`` / ``paused`` / ``failed`` / ``syncing``）。

    - ``paused``（额度耗尽，**文案明确**）→ 次日额度窗口打开后自动续传
    - ``failed``（泛化失败，认不出是不是额度）→ **默认跳过**（定时作业不
      自动重试——失败也扣额度，自动重试就是白扣）；**手动触发传
      ``include_failed=True`` 纳入**——用户盯着、明确要重试（人工意志）。
      人工「重试」按钮（:func:`aggregate_staging.retry_staging`）把行重置
      为 ``pending``，两条入口也都会捡起来
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
                target_dir_id="",
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
        if row.state == STAGING_FAILED and not include_failed:
            # ⚠️ **失败不自动重试**（2026-11-15 决策，2026-11-18 修订）——
            # 泛化失败认不出是不是额度问题，而**失败也扣额度** → 无人值守的
            # 定时作业不自动重试。**手动「立即归拢」传 include_failed=True
            # 纳入**（用户盯着、明确要重试）；逐行「重试」按钮仍可用。
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
        driver_id = int(driver_row.id)

    driver = build_driver(
        "guangya",
        credential,
        on_token_refresh=guangya_refresh_callback(driver_id),
    )
    try:
        items = await driver.list_active_offline_tasks()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("读取在途任务失败（本轮跳过 syncing 行，保守不碰）：%s", exc)
        return None
    return {str(item.get("taskId") or "") for item in items}


async def _pause_rows(rows: list[_Row], reason: str) -> int:
    """把**指定**的行转 ``paused``（撞上额度墙）。返回转成功的条数。

    ⚠️ **只暂停云下载通道的行**——分享转存通道**不受云添加额度影响**，
    早前整组连坐暂停会把附件也拖到次日（2026-11-17 修）。

    含 ``syncing``——撞墙时**当前行**正处在 syncing（提交被拒），它也得
    一起暂停，否则下次作业会把它当「在途任务已消失」重新捡起来。
    """
    if not rows:
        return 0
    ids = [int(r.id) for r in rows]
    async with session_scope() as db:
        await db.execute(
            update(SecondaryStaging)
            .where(
                SecondaryStaging.id.in_(ids),
                SecondaryStaging.state.in_((STAGING_PENDING, STAGING_SYNCING)),
            )
            .values(state=STAGING_PAUSED, last_error=reason, updated_at=bj_now())
        )
        await db.commit()
        # 精确计数（UPDATE 的 rowcount 在 aiosqlite 上拿不到）
        count = (
            await db.execute(
                select(func.count())
                .select_from(SecondaryStaging)
                .where(
                    SecondaryStaging.id.in_(ids),
                    SecondaryStaging.state == STAGING_PAUSED,
                    SecondaryStaging.last_error == reason,
                )
            )
        ).scalar_one()
        return int(count)


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
        driver_id = int(driver_row.id)
    return build_driver(
        "guangya",
        credential,
        on_token_refresh=guangya_refresh_callback(driver_id),
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
    # 只有服务端额度墙才会停——那是硬约束，无视不了）。
    # **含 failed 行**（2026-11-18 用户决策）：与「不限次数」同一语义——
    # 人工意志的重试不是「自动重试」，定时作业仍不碰 failed。
    await run_group(
        group_id,
        result,
        max_submissions=None,
        wait_for_lock=True,
        include_failed=True,
    )
    return result
