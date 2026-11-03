"""自动推送免费种到默认下载器（FR-? 待业务重设计编号）。

候选来源：``torrent_index`` 表（``free_type ∈ {freeleech_permanent,
freeleech_timed}``，且限时种 ``free_end_at - now > push_min_remaining_hours``）。
去重：``torrent_index.push_status`` 列（``pushed`` = 已推过）。

推送流程：``SiteAdapter.download_torrent(item_id)`` → bytes →
``infohash_from_torrent(bytes)`` 解析真 hash →
``DownloaderAdapter.add_torrent(torrent_bytes=bytes)`` →
更新 ``torrent_index.push_status = pushed``。

选种优先级（FR-1.6 增补）：**时魔未封顶的站优先**。

NexusPHP 的魔力公式是 ``每小时魔力 = B0 × (1 − e^(−A/B0))``，A = 自己的
做种 A 值总和。A 越大越逼近上限 B0，**多推一个种子的边际收益越小**；
已封顶（时魔进度 ≥ 100%）的站再推也不涨时魔。

所以推送名额（``push_max_per_run``）应优先给「离上限还远」的站：

=====  ==================  ==========================================
档位   含义                排序
=====  ==================  ==========================================
0     明确未封顶            最优先
1     无上限信号（NULL）     次之
2     已封顶                最后
=====  ==================  ==========================================

档内排序由运行时参数 ``push_within_bucket_order`` 决定：

- ``0``（默认）——按各站时魔进度升序（同进度退化为 ``last_seen_at DESC``）
- ``1`` —— 整个档内随机打乱（含站点顺序）

档内随机在 **Python 侧**做（不用 SQL ``RANDOM()``）——SQL 的 ``RANDOM()``
既不可 seed 也不可测；Python 侧用 ``random.Random``（生产不传 seed =
真随机，测试传 seed = 确定性）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, or_, select, update

from app.core.logging import get_logger
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import Site, SiteUserData, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.downloader_query import get_default_downloader
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep, JobStepItem, build_items

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = get_logger(__name__)

#: 时魔封顶档位（选种优先级；见模块 docstring）。
#:
#: - 0 = 明确未封顶（``hourly_bonus_capped = 0``）→ 最优先
#: - 1 = 无上限信号（``NULL``：该站不提供上限信号 / 还没抓到数据）→ 次之
#: - 2 = 已封顶（``= 1``）→ 最后
_BUCKET_OPEN = 0
_BUCKET_UNKNOWN = 1
_BUCKET_CAPPED = 2

#: 档内排序模式（运行时参数 ``push_within_bucket_order``）。
_ORDER_BY_PROGRESS = 0
_ORDER_RANDOM = 1


@dataclass(frozen=True, slots=True)
class PushResult:
    """单次推送作业的运行结果。"""

    selected: int = 0
    pushed: int = 0
    skipped_already_pushed: int = 0
    skipped_remaining_too_short: int = 0
    skipped_download_failed: int = 0
    skipped_parse_failed: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()
    #: 人话摘要（前端直接展示，避免前端硬编码字段名）
    summary: str = ""
    #: 子步骤明细（前端「步骤卡片」数据源）
    steps: tuple[JobStep, ...] = ()


class PushFreeService:
    """定时推送免费种到默认下载器。

    运行参数（``push_min_remaining_hours`` / ``push_max_per_run``）从
    :class:`app.services.runtime_params.RuntimeParamManager` 读取——
    每次 run() 实时取，DB 改完下次作业生效，无需重启。
    """

    def __init__(
        self,
        factory: AdapterFactory,
        *,
        rng: random.Random | None = None,
    ):
        """构造推送服务。

        :param rng: 档内随机排序用的随机源。**生产传 None**（每次真随机）；
            测试传 ``random.Random(42)`` 可得到确定性结果。
        """
        self._factory = factory
        self._rng = rng

    async def run(self) -> PushResult:
        """执行一次推送作业。"""
        # 1) 找默认下载器
        default_dl = await get_default_downloader()
        if default_dl is None:
            logger.warning("push_free: 未配置默认下载器，跳过本次作业")
            return PushResult(
                summary="未配置默认下载器",
                steps=(
                    JobStep("查询默认下载器", "fail", 0, "未配置默认下载器"),
                ),
            )
        downloader_id = default_dl.id

        # 2) 选候选
        async with session_scope() as session:
            candidates = await self._select_candidates(session, downloader_id)
            bucket_counts = await self._site_bucket_counts(session)

        from app.services.runtime_params import get_runtime_params

        order_mode = get_runtime_params().get_int("push_within_bucket_order")
        bucket_open, bucket_unknown, bucket_capped, bucket_items = bucket_counts
        bucket_step = JobStep(
            "按时魔封顶排序",
            "ok",
            bucket_open + bucket_unknown + bucket_capped,
            f"未封顶 {bucket_open} 站，无信号 {bucket_unknown} 站，"
            f"已封顶 {bucket_capped} 站"
            f"（档内：{'随机' if order_mode == _ORDER_RANDOM else '按进度升序'}）",
            items=bucket_items,
        )

        if not candidates:
            logger.info("push_free: 无新候选")
            return PushResult(
                summary="无新候选",
                steps=(
                    JobStep("查询默认下载器", "ok", 1, default_dl.name),
                    bucket_step,
                    JobStep("查询候选免费种", "skip", 0, "无新候选"),
                ),
            )

        # 3) 逐个推送（经 task_queue 入队 → claim → 执行 → mark，
        #    崩溃后由 drain_task_queue 作业重试；见 app.services.task_queue 模块头）
        pushed = 0
        skipped_dup = 0
        skipped_dl_failed = 0
        skipped_parse = 0
        failed = 0
        retried = 0
        errors: list[str] = []
        # 逐种明细（前端「步骤卡片」展开后可见）——回答"推了哪些 / 哪些失败"
        push_items: list[JobStepItem] = []
        # 候选明细（查询候选免费种步骤）
        candidate_items: list[JobStepItem] = [
            JobStepItem(
                c.name or c.item_id or f"#{c.index_id}",
                "skip",
                None,
                f"站点 {c.site_id} · {c.free_type or 'unknown'}",
            )
            for c in candidates
        ]

        from app.services.runtime_params import get_runtime_params
        from app.services.task_executor import PushItemError, execute_add_torrent
        from app.services.task_queue import (
            TaskKind,
            TaskQueueService,
            make_idem_key,
        )

        # 一次性取所有运行时参数，避免循环内/外重复取值导致
        # "限频值"与"展示值"不一致（AGENTS.md 参数内存-DB 顺序一致关注点）。
        rp = get_runtime_params()
        interval_ms = rp.get_int("push_download_interval_ms")
        min_remaining_h = rp.get_int("push_min_remaining_hours")
        first = True

        for c in candidates:
            # 3.0) 站点限频保护：首个不等待，后续每次下载前 sleep
            if not first and interval_ms > 0:
                await asyncio.sleep(interval_ms / 1000)
            first = False

            # 3.1) 入队（幂等）——同一 (site_id, item_id) 只入队一次。
            # idem 命中说明：已推过（done）/ 在推（running）/ 待重试（pending/failed）
            # → 本作业不重复处理，drain_task_queue 作业负责重试。
            idem_key = make_idem_key(
                TaskKind.ADD_TORRENT,
                site_id=c.site_id,
                info_hash=c.item_id,
                extra=f"dl={downloader_id}",
            )
            payload: dict[str, object] = {
                "index_id": c.index_id,
                "downloader_id": downloader_id,
            }
            async with session_scope() as session:
                tq = TaskQueueService(session)
                enq = await tq.enqueue(TaskKind.ADD_TORRENT, payload, idem_key=idem_key)
                if not enq.created:
                    skipped_dup += 1
                    push_items.append(
                        JobStepItem(
                            c.name or c.item_id or f"#{c.index_id}",
                            "skip",
                            None,
                            "幂等跳过（队列中已有等价任务）",
                        )
                    )
                    continue
                task = await tq.claim_one(enq.task_id)
            if task is None:
                skipped_dup += 1
                push_items.append(
                    JobStepItem(
                        c.name or c.item_id or f"#{c.index_id}",
                        "skip",
                        None,
                        "幂等跳过（被其他执行者认领）",
                    )
                )
                continue

            label = c.name or c.item_id or f"#{c.index_id}"
            # 3.2) 执行（claim 已落库——此后崩溃，任务停在 running，
            #      超 STALE_RUNNING_MINUTES 后被 drain 作业回收重试）
            try:
                await execute_add_torrent(payload, self._factory)
            except PushItemError as exc:
                async with session_scope() as session:
                    tq = TaskQueueService(session)
                    if exc.category == "fatal":
                        await tq.mark_failed_forever(task.id, str(exc))
                        final_state = "failed"
                    else:
                        final_state = await tq.mark_failed(task.id, str(exc))
                errors.append(f"site={c.site_id} item={c.item_id}: {exc}")
                await self._mark_push_failed(c.index_id)
                if exc.category == "download":
                    skipped_dl_failed += 1
                    push_items.append(
                        JobStepItem(label, "fail", None, f"下载失败：{exc}")
                    )
                elif exc.category == "parse":
                    skipped_parse += 1
                    push_items.append(
                        JobStepItem(label, "fail", None, f"解析失败：{exc}")
                    )
                elif final_state == "failed":
                    failed += 1
                    push_items.append(
                        JobStepItem(label, "fail", None, str(exc))
                    )
                else:
                    retried += 1
                    push_items.append(
                        JobStepItem(label, "warn", None, f"退避重试：{exc}")
                    )
                continue
            except Exception as exc:
                async with session_scope() as session:
                    tq = TaskQueueService(session)
                    final_state = await tq.mark_failed(
                        task.id, f"{type(exc).__name__}: {exc}"
                    )
                failed += 1
                errors.append(f"site={c.site_id} item={c.item_id}: {exc}")
                await self._mark_push_failed(c.index_id)
                push_items.append(
                    JobStepItem(
                        label, "fail", None, f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            async with session_scope() as session:
                tq = TaskQueueService(session)
                await tq.mark_done(task.id)
            pushed += 1
            push_items.append(JobStepItem(label, "ok", None, "推送成功"))

        push_items, push_items_total = build_items(push_items)
        candidate_items, candidate_items_total = build_items(candidate_items)

        result = PushResult(
            selected=len(candidates),
            pushed=pushed,
            skipped_already_pushed=skipped_dup,
            skipped_download_failed=skipped_dl_failed,
            skipped_parse_failed=skipped_parse,
            failed=failed,
            errors=tuple(errors[:10]),
            summary=(
                f"选中 {len(candidates)}，推送 {pushed} 成功"
                + (f"，{failed} 失败" if failed else "")
                + (f"，{retried} 退避重试" if retried else "")
            ),
            steps=(
                JobStep("查询默认下载器", "ok", 1, default_dl.name),
                bucket_step,
                JobStep(
                    "查询候选免费种",
                    "ok",
                    len(candidates),
                    f"{len(candidates)} 个候选，限时剩余 ≥ {min_remaining_h}h",
                    items=candidate_items,
                    items_total=candidate_items_total,
                ),
                JobStep(
                    "下载 .torrent",
                    "warn" if skipped_dl_failed or skipped_parse else "ok",
                    pushed + skipped_dl_failed + skipped_parse,
                    f"{pushed} 成功，{skipped_dl_failed} 限频失败，{skipped_parse} 解析失败",
                ),
                JobStep(
                    "推送到下载器",
                    "warn" if failed else "ok",
                    pushed,
                    f"{pushed} 成功，{failed} 失败，{retried} 退避重试，"
                    f"{skipped_dup} 幂等跳过",
                    items=push_items,
                    items_total=push_items_total,
                ),
            ),
        )
        logger.info(
            "push_free: 选中 %d，推送 %d，跳过重复 %d，下载失败 %d，"
            "解析失败 %d，推送失败 %d，退避重试 %d",
            result.selected, result.pushed, result.skipped_already_pushed,
            result.skipped_download_failed, result.skipped_parse_failed,
            result.failed, retried,
        )
        return result

    async def _select_candidates(
        self, session: AsyncSession, downloader_id: int
    ) -> list[_Candidate]:
        """选候选免费种：未推过 + 剩余时间满足阈值 + **按时魔封顶档排序**。

        排序（见模块 docstring）：主键是时魔封顶档（未封顶 → 无信号 → 已封顶），
        档内按运行时参数 ``push_within_bucket_order``：

        - 0（默认）——各站时魔进度升序（同进度退化为 ``last_seen_at DESC``）
        - 1 —— 整个档内随机打乱（含站点顺序）

        注：不再从 torrent_index 读 URL——推送时实时调
        :meth:`SiteAdapter.download_torrent` 获取 .torrent bytes。
        """
        now = bj_now()
        # 运行时参数（DB 持久化，UI 可调；详见 app.services.runtime_params）
        from app.services.runtime_params import get_runtime_params

        rp = get_runtime_params()
        min_remaining = timedelta(hours=rp.get_int("push_min_remaining_hours"))
        threshold = format_bj(bj_now_dt() + min_remaining)
        order_mode = rp.get_int("push_within_bucket_order")

        # 档位表达式：未封顶 → 无信号 → 已封顶
        bucket = case(
            (SiteUserData.hourly_bonus_capped.is_(False), _BUCKET_OPEN),
            (SiteUserData.hourly_bonus_capped.is_(None), _BUCKET_UNKNOWN),
            else_=_BUCKET_CAPPED,
        )

        # 候选集：torrent_index 上 free_type ∈ 真免费/限时免费 且 item_id 非空
        #         且 push_status 未推过（NULL 或 != pushed）
        stmt = (
            select(
                TorrentIndex.id,
                TorrentIndex.site_id,
                TorrentIndex.item_id,
                TorrentIndex.name,
                TorrentIndex.free_type,
                TorrentIndex.free_end_at,
                bucket.label("bonus_bucket"),
            )
            # LEFT JOIN：站点没有 site_user_data 行（没抓过 / 抓失败）时
            # hourly_bonus_capped 为 NULL → 落「无信号」档，行为与改动前一致。
            .outerjoin(SiteUserData, SiteUserData.site_id == TorrentIndex.site_id)
            .where(
                # 显式白名单：永久免费 + 限时免费 + 2X 限时
                # 退还（REFUND）/折扣（NEUTRAL）/未知（UNKNOWN）均不自动推送
                or_(
                    TorrentIndex.free_type == FreeType.PERMANENT,
                    TorrentIndex.free_type == FreeType.TIMED,
                    TorrentIndex.free_type == FreeType.TWOU_TIMED,
                ),
                TorrentIndex.item_id.is_not(None),
                or_(
                    TorrentIndex.push_status.is_(None),
                    TorrentIndex.push_status != PushStatus.PUSHED,
                ),
            )
            .order_by(
                bucket,
                # 进度升序：离上限最远的站先（progress 是站点级，同站候选同值）
                SiteUserData.hourly_bonus_progress.asc().nulls_last(),
                TorrentIndex.last_seen_at.desc(),
            )
        )
        rows = list((await session.execute(stmt)).all())

        # 档内随机（模式 1）：把每个档的候选整组打乱。
        #
        # 为什么在 Python 侧做而不用 SQL ``RANDOM()``：SQL 的 RANDOM() 既不可
        # seed 也不可测；Python 侧用 random.Random——生产传 None = 真随机，
        # 测试传 seed = 确定性。
        #
        # 档内打乱**在过滤循环之前**做：循环仍会在凑满 push_max_per_run 时
        # break，所以下面的「已推过」检查（每候选一次 DB 查询）次数不变——
        # 不会因为扫全表变成 N+1。
        if order_mode == _ORDER_RANDOM:
            rows = _shuffle_within_buckets(rows, self._rng)

        candidates: list[_Candidate] = []
        for row in rows:
            idx_id, site_id, item_id, name, free_type, free_end_at, _bucket = row
            # is_not_null 已 WHERE 过滤，但 assert 在 `python -O` 下被剥离；
            # 用显式 if 跳过更安全（防御类型契约变化）。
            if item_id is None:
                continue
            # 限时种：未过期 且 剩余时间 ≥ 阈值 → 入选
            # 显式拒绝已过期（<= now），避免下载器对一个已失效窗口做无意义拉取
            if (free_type == FreeType.TIMED
                    and (free_end_at is None
                         or free_end_at <= now
                         or free_end_at < threshold)):
                continue
            # 永久免费：free_end_at 必为 None，永远入选
            # 已经推送过则跳过
            if await self._is_already_pushed(session, site_id, item_id):
                continue
            candidates.append(
                _Candidate(
                    index_id=idx_id,
                    site_id=site_id,
                    item_id=item_id,
                    name=name,
                    free_type=free_type,
                    free_end_at=free_end_at,
                )
            )
            if len(candidates) >= rp.get_int("push_max_per_run"):
                break
        return candidates

    async def _site_bucket_counts(
        self, session: AsyncSession
    ) -> tuple[int, int, int, list[JobStepItem]]:
        """统计启用站点按时魔封顶档的分布（未封顶 / 无信号 / 已封顶）。

        用于作业步骤卡片——让用户看到「这次按什么优先级排的」。

        按**站点**统计（不是候选数）：候选数会随抓取波动，站点数才反映
        「有多少个站还有时魔可拿」。

        :return: ``(未封顶数, 无信号数, 已封顶数, 逐站明细)``。明细让用户
            能看到「哪个站在哪一档」，而不只是三个数字。
        """
        bucket = case(
            (SiteUserData.hourly_bonus_capped.is_(False), _BUCKET_OPEN),
            (SiteUserData.hourly_bonus_capped.is_(None), _BUCKET_UNKNOWN),
            else_=_BUCKET_CAPPED,
        )
        stmt = (
            select(bucket.label("bucket"), func.count().label("n"))
            .select_from(Site)
            .outerjoin(SiteUserData, SiteUserData.site_id == Site.id)
            .where(Site.enabled.is_(True))
            .group_by(bucket)
        )
        counts = {_BUCKET_OPEN: 0, _BUCKET_UNKNOWN: 0, _BUCKET_CAPPED: 0}
        for bucket_value, n in (await session.execute(stmt)).all():
            counts[int(bucket_value)] = int(n)

        # 逐站明细：站名 + 档位 + 时魔进度
        detail_stmt = (
            select(
                Site.name,
                bucket.label("bucket"),
                SiteUserData.hourly_bonus_progress,
            )
            .select_from(Site)
            .outerjoin(SiteUserData, SiteUserData.site_id == Site.id)
            .where(Site.enabled.is_(True))
            .order_by(bucket, Site.id)
        )
        items: list[JobStepItem] = []
        for site_name, bucket_value, progress in (
            await session.execute(detail_stmt)
        ).all():
            b = int(bucket_value)
            if b == _BUCKET_OPEN:
                label = "未封顶"
                status = "ok"
            elif b == _BUCKET_UNKNOWN:
                label = "无信号"
                status = "skip"
            else:
                label = "已封顶"
                status = "warn"
            items.append(
                JobStepItem(
                    str(site_name),
                    status,
                    None,
                    f"{label}"
                    + (
                        f"，进度 {float(progress) * 100:.0f}%"
                        if progress is not None
                        else ""
                    ),
                )
            )

        return (
            counts[_BUCKET_OPEN],
            counts[_BUCKET_UNKNOWN],
            counts[_BUCKET_CAPPED],
            items,
        )

    async def _is_already_pushed(
        self, session: AsyncSession, site_id: int, item_id: str
    ) -> bool:
        """是否已推过：torrent_index 上 push_status == pushed。"""
        stmt = select(TorrentIndex.id).where(
            TorrentIndex.site_id == site_id,
            TorrentIndex.item_id == item_id,
            TorrentIndex.push_status == PushStatus.PUSHED,
        )
        return (await session.execute(stmt)).first() is not None

    async def _mark_push_failed(self, index_id: int) -> None:
        """标记推送失败（``push_status = failed``）。

        让失败可追溯——原先失败只落在作业的 errors 列表里，DB 无痕迹，
        ``push_status`` 的 ``failed`` 取值从未被写入（死语义）。

        不会覆盖已 ``pushed`` 的行：并发场景下另一作业可能刚推成功，
        覆盖会把成功记录改成失败。

        注：候选查询条件是 ``push_status IS NULL OR != pushed``，故
        ``failed`` 状态的条目**下轮仍会被选中重试**（重试语义正确）。
        """
        try:
            async with session_scope() as session:
                # push_status != PUSHED 对 NULL 求值为 NULL（不匹配），
                # 故用 or_(NULL, != PUSHED) 显式覆盖"从未推过"的行
                await session.execute(
                    update(TorrentIndex)
                    .where(
                        TorrentIndex.id == index_id,
                        or_(
                            TorrentIndex.push_status.is_(None),
                            TorrentIndex.push_status != PushStatus.PUSHED,
                        ),
                    )
                    .values(push_status=PushStatus.FAILED)
                )
        except Exception as exc:
            # 标记失败本身失败不应中断整个推送作业；但需记录便于追溯——
            # 否则 push_status 仍是 NULL 与实际"推送失败但标记未落"状态不符。
            logger.warning(
                "标记推送失败状态失败：index_id=%d（push_status 可能仍为 NULL）：%s",
                index_id, exc,
            )


def _shuffle_within_buckets[RowT: Sequence[Any]](
    rows: list[RowT], rng: random.Random | None
) -> list[RowT]:
    """把每个时魔档位内的候选整组打乱（档位之间的顺序不变）。

    SQL 的主排序是档位，所以同一档的行在结果里**天然连续**——按档切分
    打乱即可，档与档之间的优先级（未封顶 → 无信号 → 已封顶）不受影响。

    :param rows: 查询结果行，末位是档位（``bonus_bucket``）。
    :param rng: 随机源；``None`` = 用系统熵新建一个（生产真随机）。
    """
    if not rows:
        return rows
    source = rng if rng is not None else random.Random()
    out: list[RowT] = []
    start = 0
    total = len(rows)
    while start < total:
        bucket = rows[start][-1]
        end = start + 1
        while end < total and rows[end][-1] == bucket:
            end += 1
        group = list(rows[start:end])
        source.shuffle(group)
        out.extend(group)
        start = end
    return out


@dataclass(frozen=True, slots=True)
class _Candidate:
    """推送候选（仅内部用）。"""

    index_id: int
    site_id: int
    item_id: str
    name: str
    free_type: str | None = None
    free_end_at: str | None = None
