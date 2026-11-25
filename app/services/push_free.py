"""自动推送免费种到默认下载器（FR-? 待业务重设计编号）。

候选来源：``torrent_index`` 表（``free_type ∈ {freeleech_permanent,
freeleech_timed}``，且限时种 ``free_end_at - now > push_min_remaining_hours``）。
去重：``torrent_index.push_status`` 列（``pushed`` = 已推过）。

站点范围：**只推启用站点**（``sites.enabled = 1``）。站点停用只停止拉取，
**不删** ``torrent_index`` 里的快照——不过滤就会出现「站点已停用却还在推」。

推送流程：``SiteAdapter.download_torrent(item_id)`` → bytes →
``infohash_from_torrent(bytes)`` 解析真 hash →
``DownloaderAdapter.add_torrent(torrent_bytes=bytes)`` →
更新 ``torrent_index.push_status = pushed``。

排序：按 ``torrent_index.last_seen_at DESC``（新条目优先）。

注：曾按「时魔封顶档」分档排序（未封顶 / 无信号 / 已封顶），档内再按运行时
参数 ``push_within_bucket_order`` 选择「时魔进度升序」或「随机打乱」。该特性
随「时魔」功能整体删除（2026-11-20），对应的运行时参数、档位常量与
``SiteUserData`` 外连接一并移除——**当前只剩上面这一条排序口径**。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update

from app.core.logging import get_logger
from app.core.timeutils import bj_now, bj_now_dt, format_bj
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.downloader_query import get_default_downloader
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep, JobStepItem, build_items

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = get_logger(__name__)

#: 候选**新鲜度下限**（小时）：``torrent_index.last_seen_at`` 早于这个时长的
#: 条目一律不推送。
#:
#: 为什么需要：某站凭据失效期间，库里会攒下大量**陈旧快照**——它们的
#: 「免费」状态是失效前的旧数据，早就不可信（2026-11-08 prod：ourbits
#: 凭据过期 10 天，1557 条 10 天前的快照仍在库里，其中永久免费种
#: ``free_end_at IS NULL`` **永远入选**候选 → 一旦别的站候选不够填满
#: push_max_per_run，就会把「当前实为收费」的种子推给下载器）。
#:
#: 24 小时 = 拉取作业周期（10 分钟）的 144 倍，留足容错；站点真停摆时
#: 它的候选会在一天内自动退出推送池。
_FRESHNESS_WINDOW_HOURS = 24


def _freshness_cutoff() -> str:
    """候选新鲜度下限（北京时间字符串，格式与 ``last_seen_at`` 一致）。

    ``last_seen_at`` 是固定格式的北京时间字符串（``YYYY-MM-DD HH:MM:SS``），
    字典序 == 时间序，可直接用 SQL 字符串比较。
    """
    return format_bj(bj_now_dt() - timedelta(hours=_FRESHNESS_WINDOW_HOURS))


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
    #: 推送前统计到的下载器在途下载数（未启用水位上限时为 None）
    active_downloads: int | None = None
    #: 本次生效的在途下载水位上限（0 = 不限）
    max_active_downloads: int = 0
    #: True 表示因下载器在途数已达上限而跳过本轮推送
    skipped_by_capacity: bool = False
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

    async def run(self, *, active_downloads: int | None = None) -> PushResult:
        """执行一次推送作业。

        :param active_downloads: 可选，默认下载器当前在途下载数（由合并作业的
            监测阶段透传，避免同轮重复调用 ``list_torrents()``）。为 ``None``
            且启用了在途水位上限时，本方法会主动拉取下载器任务统计。
        """
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

        # 1.5) 计算动态推送配额（按在途下载水位线补位，防堵塞）
        from app.services.monitor import is_active_download
        from app.services.runtime_params import get_runtime_params

        rp = get_runtime_params()
        max_per_run = rp.get_int("push_max_per_run")
        max_active = rp.get_int("push_max_active_downloads")
        interval_ms = rp.get_int("push_download_interval_ms")
        min_remaining_h = rp.get_int("push_min_remaining_hours")

        active_count: int | None = None
        quota = max_per_run
        capacity_step: JobStep | None = None
        if max_active > 0:
            if active_downloads is not None:
                active_count = active_downloads
            else:
                try:
                    adapter = self._factory.downloader(default_dl)
                    infos = await adapter.list_torrents()
                except Exception as exc:
                    logger.warning("push_free: 查询下载器在途任务失败（不盲推）：%s", exc)
                    return PushResult(
                        failed=1,
                        errors=(f"list_torrents: {exc}",),
                        max_active_downloads=max_active,
                        summary=f"查询下载器在途任务失败：{exc}",
                        steps=(
                            JobStep("查询默认下载器", "ok", 1, default_dl.name),
                            JobStep("检查下载器水位", "fail", 0, str(exc)),
                        ),
                    )
                active_count = sum(1 for info in infos if is_active_download(info))

            available = max(0, max_active - active_count)
            quota = min(max_per_run, available)
            if quota <= 0:
                logger.info(
                    "push_free: 下载器在途 %d/%d，已达上限暂缓推送",
                    active_count,
                    max_active,
                )
                return PushResult(
                    active_downloads=active_count,
                    max_active_downloads=max_active,
                    skipped_by_capacity=True,
                    summary=f"在途下载 {active_count}/{max_active}，已达上限暂缓推送",
                    steps=(
                        JobStep("查询默认下载器", "ok", 1, default_dl.name),
                        JobStep(
                            "检查下载器水位",
                            "skip",
                            active_count,
                            f"在途 {active_count}/{max_active}，无空闲槽位",
                        ),
                    ),
                )
            capacity_step = JobStep(
                "检查下载器水位",
                "ok",
                active_count,
                f"在途 {active_count}/{max_active}，本次最多推 {quota} 个",
            )

        # 2) 选候选
        async with session_scope() as session:
            candidates = await self._select_candidates(
                session, downloader_id, limit=quota
            )

        if not candidates:
            logger.info("push_free: 无新候选")
            idle_steps: list[JobStep] = [
                JobStep("查询默认下载器", "ok", 1, default_dl.name)
            ]
            if capacity_step is not None:
                idle_steps.append(capacity_step)
            idle_steps.append(JobStep("查询候选免费种", "skip", 0, "无新候选"))
            return PushResult(
                active_downloads=active_count,
                max_active_downloads=max_active,
                summary="无新候选",
                steps=tuple(idle_steps),
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

        from app.services.task_executor import PushItemError, execute_add_torrent
        from app.services.task_queue import (
            TaskKind,
            TaskQueueBusyError,
            TaskQueueService,
            claim_with_retry,
            enqueue_with_retry,
            make_idem_key,
        )

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
            label = c.name or c.item_id or f"#{c.index_id}"
            # 3.1) 入队（幂等）+ 认领——撞 SQLite 写锁时退避重试。
            #
            # 2026-11-12 prod 事故：扫描作业批量写库期间持有写锁，这里的
            # INSERT 等满 busy_timeout(10s) 仍失败 → **整轮推送炸掉**。
            # 入队是核心业务（不能像直链缓存那样静默放弃）→ 退避重试；
            # 重试耗尽则该候选记失败明细并 **continue 下一个候选**，
            # 幂等键保证下轮 cron 自动补推、不重复入队。
            try:
                enq = await enqueue_with_retry(
                    TaskKind.ADD_TORRENT, payload, idem_key=idem_key
                )
                if not enq.created:
                    skipped_dup += 1
                    push_items.append(
                        JobStepItem(
                            label,
                            "skip",
                            None,
                            "幂等跳过（队列中已有等价任务）",
                        )
                    )
                    continue
                claimed = await claim_with_retry(enq.task_id)
            except TaskQueueBusyError as exc:
                failed += 1
                errors.append(f"site={c.site_id} item={c.item_id}: {exc}")
                push_items.append(
                    JobStepItem(
                        label,
                        "fail",
                        None,
                        "入队失败：数据库忙，下轮自动补推",
                    )
                )
                continue
            if not claimed:
                skipped_dup += 1
                push_items.append(
                    JobStepItem(
                        label,
                        "skip",
                        None,
                        "幂等跳过（被其他执行者认领）",
                    )
                )
                continue
            task_id = enq.task_id

            # 3.2) 执行（claim 已落库——此后崩溃，任务停在 running，
            #      超 STALE_RUNNING_MINUTES 后被 drain 作业回收重试）
            try:
                await execute_add_torrent(payload, self._factory)
            except PushItemError as exc:
                async with session_scope() as session:
                    tq = TaskQueueService(session)
                    if exc.category == "fatal":
                        await tq.mark_failed_forever(task_id, str(exc))
                        final_state = "failed"
                    else:
                        final_state = await tq.mark_failed(task_id, str(exc))
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
                        task_id, f"{type(exc).__name__}: {exc}"
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
                await tq.mark_done(task_id)
            pushed += 1
            push_items.append(JobStepItem(label, "ok", None, "推送成功"))

        push_items, push_items_total = build_items(push_items)
        candidate_items, candidate_items_total = build_items(candidate_items)

        steps_list: list[JobStep] = [
            JobStep("查询默认下载器", "ok", 1, default_dl.name),
        ]
        if capacity_step is not None:
            steps_list.append(capacity_step)
        steps_list.extend(
            [
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
            ]
        )

        result = PushResult(
            selected=len(candidates),
            pushed=pushed,
            skipped_already_pushed=skipped_dup,
            skipped_download_failed=skipped_dl_failed,
            skipped_parse_failed=skipped_parse,
            failed=failed,
            errors=tuple(errors[:10]),
            active_downloads=active_count,
            max_active_downloads=max_active,
            summary=(
                f"选中 {len(candidates)}，推送 {pushed} 成功"
                + (f"，{failed} 失败" if failed else "")
                + (f"，{retried} 退避重试" if retried else "")
            ),
            steps=tuple(steps_list),
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
        self,
        session: AsyncSession,
        downloader_id: int,
        *,
        limit: int | None = None,
    ) -> list[_Candidate]:
        """选候选免费种：未推过 + 剩余时间满足阈值 + **按新条目优先排序**。

        排序：按 ``last_seen_at DESC``（新条目优先）。

        注：不再从 torrent_index 读 URL——推送时实时调
        :meth:`SiteAdapter.download_torrent` 获取 .torrent bytes。
        """
        now = bj_now()
        # 运行时参数（DB 持久化，UI 可调；详见 app.services.runtime_params）
        from app.services.runtime_params import get_runtime_params

        rp = get_runtime_params()
        max_count = limit if limit is not None else rp.get_int("push_max_per_run")
        if max_count <= 0:
            return []
        min_remaining = timedelta(hours=rp.get_int("push_min_remaining_hours"))
        threshold = format_bj(bj_now_dt() + min_remaining)

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
            )
            # INNER JOIN sites：只推**启用**站点的种子。
            #
            # 为什么必须：站点停用（sites.enabled=0）只停止拉取，**不删**
            # torrent_index 里的快照——若不过滤，停用后旧快照仍会被选中
            # （在 24h 新鲜度窗口内），出现「站点已停用却还在推」的故障。
            # INNER JOIN 顺带兜住站点已删的孤儿行（正常路径下不会存在：
            # 删站点会级联删 torrent_index）。
            .join(Site, Site.id == TorrentIndex.site_id)
            # LEFT JOIN：站点没有 site_user_data 行（没抓过 / 抓失败）时
            .where(
                # 只推启用站点（停用站点的种子一律不推）
                Site.enabled.is_(True),
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
                # 陈旧快照不推（凭据失效期间攒下的条目免费状态不可信）
                TorrentIndex.last_seen_at >= _freshness_cutoff(),
            )
            .order_by(TorrentIndex.last_seen_at.desc())
        )
        rows = list((await session.execute(stmt)).all())

        candidates: list[_Candidate] = []
        for row in rows:
            idx_id, site_id, item_id, name, free_type, free_end_at = row
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
            if len(candidates) >= max_count:
                break
        return candidates

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


@dataclass(frozen=True, slots=True)
class _Candidate:
    """推送候选（仅内部用）。"""

    index_id: int
    site_id: int
    item_id: str
    name: str
    free_type: str | None = None
    free_end_at: str | None = None
