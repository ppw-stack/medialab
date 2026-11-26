"""免费窗口监测作业（FR-3.4 巡检 + 临近截止自动暂停 / 删除）。

流程：
1. 拉默认下载器全量任务（``adapter.list_torrents()``）—— 拉下载器是权威
2. 提取 qB 任务 hash 列表，按此列表查 ``torrent_index`` 当前状态
3. 遍历下载器中的每个种子：若 db 命中 ``push_status=pushed`` → 按 db 当前
   ``free_type`` 判断处理：
   - 当前是限时种（TIMED/TWOU_TIMED）→ 应用窗口规则
   - 当前是永久种（PERMANENT）→ 永不处理
   - 当前是非免费（neutral_leech / unknown / 其他）→ 立即处理

**动作由运行参数 ``monitor_delete_expired`` 决定**（0=仅暂停，默认；
1=删任务 + 删磁盘数据）。两种动作的**判定范围完全一致**——
「该暂停的」就是「该删的」，差别只在执行动作。

删除模式的三条约束：

1. **只删正在跑的**：已完成与已暂停的任务**跳过**
   （沿用暂停路径的跳过逻辑）。已完成的种子数据已下完，删掉是净损失。
   「已完成」用**字节口径**判定（``completed_bytes >= size``），
   **不能用** ``progress >= 1.0``——校验中 progress 是「校验进度」
   不是完成度（详见 :func:`_is_completed`）。
2. **保留 ``torrent_index`` 行**：删除只作用于下载器侧，索引行与
   ``push_status=pushed`` 保持不变。``push_free`` 的候选条件要求
   ``push_status IS NULL OR != pushed``，故该种**不会**被重新推送
   ——否则「推送 → 删除 → 再推送」会无限循环。
3. **删磁盘数据不可逆**：走 ``delete_data=True``，且必须记审计日志
   （AGENTS.md：删磁盘数据单独开关并记审计日志）。

**校验中（CHECKING）的种子两种模式都跳过**：

- 暂停模式：等校验完成后由下一轮巡检处理（作业每 10 分钟一轮）
- 删除模式：**绝不删除**校验中的种子——校验期完成度判定不可靠，
  而删磁盘数据不可逆

不写 DB。``torrent_index`` 中已推送但不在下载器中的种子不用管
（可能 qB 自动删除 / 用户手动删，不强制标记）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.adapters.downloaders.base import DownloaderAdapter, TorrentState
from app.core.logging import get_logger
from app.core.metrics import (
    cleanup_actions,
    free_seed_outcomes,
)
from app.core.metrics import state as metric_state
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.downloader_query import get_default_downloader
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep, JobStepItem, build_items

if TYPE_CHECKING:
    from app.adapters.downloaders.base import TorrentInfo

logger = get_logger(__name__)


def is_completed(info: TorrentInfo) -> bool:
    """是否「已完成」（数据已下完）。

    **必须用字节口径，不能用 ``progress >= 1.0``**——校验中
    （recheck）时 qBittorrent 的 ``progress`` 返回的是**校验进度**而非
    下载完成度（源码 ``TorrentImpl::progress()``：

    .. code-block:: cpp

        if (isChecking()) return m_nativeStatus.progress;

    libtorrent 侧该值 = 已校验分片数 / 总分片数）。于是 qB 重启后
    recheck 的**已完成做种种子** ``progress`` 从 0 爬升，
    ``progress >= 1.0`` 判定失效 → 被当成「未完成」→ 删除模式下会
    删掉早已下载完的磁盘数据（净损失）。

    字节口径不受校验影响：``completed_bytes`` 是已校验有效的字节数
    （qB 取 ``completed``，TR 取 ``haveValid``），与 ``size`` 同口径。

    ``size <= 0`` 时无法判定（无元数据 / 元数据未到），按「未完成」处理
    ——保守方向与原 ``progress`` 判定一致。
    """
    if info.size <= 0:
        return False
    return info.completed_bytes >= info.size


#: 兼容旧测试/内部引用别名
_is_completed = is_completed


def is_active_download(info: TorrentInfo) -> bool:
    """是否为占槽的「在途下载」任务（推送水位线与监测共享单一口径）。

    口径：未完成（字节口径）且处于下载相关状态
    （``DOWNLOADING`` / ``STALLED_DL`` / ``CHECKING``，含下载器排队 ``queuedDL``）。
    排除已完成做种、已暂停与致命错误任务。
    """
    return not is_completed(info) and info.state.is_downloading


def _stalled_or_slow_reason(
    info: TorrentInfo,
    *,
    now_ts: float,
    timeout_minutes: int,
    min_dlspeed_kbps: int,
) -> str | None:
    """判断是否命中「停滞/龟速超时」堵塞条件，命中返回中文原因，否则 None。"""
    if timeout_minutes <= 0:
        return None
    if info.is_queued or info.added_at is None:
        return None
    elapsed = int(now_ts - info.added_at)
    if elapsed < timeout_minutes * 60:
        return None
    elapsed_min = elapsed // 60
    if info.state == TorrentState.STALLED_DL:
        return f"下载停滞超 {elapsed_min} 分钟"
    if info.state == TorrentState.DOWNLOADING:
        if min_dlspeed_kbps <= 0:
            if info.dlspeed <= 0:
                return f"0 速停滞超 {elapsed_min} 分钟"
        elif info.dlspeed < min_dlspeed_kbps * 1024:
            return (
                f"下载龟速（{info.dlspeed // 1024} KB/s < "
                f"{min_dlspeed_kbps} KB/s）超 {elapsed_min} 分钟"
            )
    return None


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """单次监测作业的运行结果。"""

    scanned: int = 0
    matched: int = 0
    skipped_unmatched: int = 0
    skipped_far_from_window: int = 0
    skipped_already_completed: int = 0
    skipped_already_paused: int = 0
    skipped_checking: int = 0
    skipped_permanent: int = 0
    skipped_refund: int = 0
    #: 致命错误（ERROR / MISSING_FILES）命中并成功处理数
    aborted_fatal: int = 0
    #: 停滞/龟速超时命中并成功处理数
    aborted_stalled: int = 0
    #: 因达到单轮异常/停滞清理上限而延至下轮处理的种子数
    skipped_action_cap: int = 0
    #: 本轮监测动作执行后，默认下载器内剩余的全局在途下载数（供推送水位复用；拉取失败时 None）
    remaining_active_downloads: int | None = None
    pre_paused: int = 0
    #: 命中条件后被**删除**的任务数（``monitor_delete_expired=1`` 时）。
    #: 为 0 表示本次没有删除——两种模式下它都是 0（暂停模式）。
    deleted: int = 0
    failed: int = 0
    errors: tuple[str, ...] = ()
    #: 人话摘要（前端直接展示，避免前端硬编码字段名）
    summary: str = ""
    #: 子步骤明细（前端「步骤卡片」数据源）
    steps: tuple[JobStep, ...] = ()


class MonitorFreeWindowService:
    """免费窗口巡检 + 临近截止自动暂停 / 删除。

    提前暂停分钟数从 :class:`app.services.runtime_params.RuntimeParamManager`
    实时读取，DB 改完下次作业生效。构造参数只用于首次 run 之前的占位。

    命中条件后的动作由 ``monitor_delete_expired`` 决定（0=仅暂停，默认；
    1=删任务 + 删磁盘数据）。见模块 docstring 的约束说明。
    """

    def __init__(self, factory: AdapterFactory) -> None:
        self._factory = factory
        self._pre_pause = timedelta(minutes=10)  # 占位，run() 内从 runtime_params 覆盖

    @staticmethod
    async def _act(
        adapter: DownloaderAdapter,
        hash_: str,
        delete_mode: bool,
        *,
        downloader_name: str = "",
        name: str = "",
        reason: str = "",
        completed_bytes: int = 0,
        size: int = 0,
        dlspeed: int = 0,
        elapsed_min: int | None = None,
    ) -> tuple[str, str | None]:
        """对单个种子执行动作（暂停或删除）。

        :param adapter: 下载器适配器。
        :param hash_: 种子 infohash（40 位 hex，由适配器校验）。
        :param delete_mode: True=删任务 + 删磁盘数据；False=仅暂停。
        :return: ``(动作名, 错误信息)``。动作名为 ``"deleted"`` /
            ``"set_pause"``；错误信息为 None 表示成功，否则是异常文本
            （调用方据此计入 failed）。
        """
        elapsed_text = f"{elapsed_min}m" if elapsed_min is not None else "unknown"
        if delete_mode:
            # 删磁盘数据不可逆 → 必须记完整结构化审计日志（AGENTS.md：删磁盘数据
            # 单独开关并记审计日志）。记录下载器、hash、种子名、触发原因、完成字节
            # 与速率，便于事后取证。
            logger.warning(
                "monitor 审计[删除任务+磁盘数据]：downloader=%s hash=%s name=%r "
                "reason=%s completed=%d/%d speed=%dB/s elapsed=%s",
                downloader_name,
                hash_,
                name,
                reason,
                completed_bytes,
                size,
                dlspeed,
                elapsed_text,
            )
            try:
                await adapter.remove([hash_], delete_data=True)
            except Exception as exc:
                return "remove", str(exc)
            return "deleted", None
        logger.info(
            "monitor 审计[暂停任务]：downloader=%s hash=%s name=%r "
            "reason=%s completed=%d/%d speed=%dB/s elapsed=%s",
            downloader_name,
            hash_,
            name,
            reason,
            completed_bytes,
            size,
            dlspeed,
            elapsed_text,
        )
        try:
            await adapter.set_pause([hash_], paused=True)
        except Exception as exc:
            return "set_pause", str(exc)
        return "set_pause", None

    async def run(self) -> MonitorResult:
        """执行一次监测作业。"""
        # 1) 查默认下载器
        default_dl = await get_default_downloader()
        if default_dl is None:
            logger.warning("monitor: 未配置默认下载器，跳过本次作业")
            return MonitorResult(
                summary="未配置默认下载器",
                steps=(JobStep("查询默认下载器", "fail", 0, "未配置默认下载器"),),
            )

        # 2) 拉下载器全量任务
        try:
            adapter = self._factory.downloader(default_dl)
            infos: list[TorrentInfo] = await adapter.list_torrents()
        except Exception as exc:
            logger.warning("monitor: 下载器 list_torrents 失败：%s", exc)
            return MonitorResult(
                failed=1,
                errors=(f"list_torrents: {exc}",),
                summary=f"拉取下载器任务失败：{exc}",
                steps=(
                    JobStep("查询默认下载器", "ok", 1, default_dl.name),
                    JobStep("拉取下载器任务", "fail", 0, str(exc)),
                ),
            )

        scanned = len(infos)
        if scanned == 0:
            logger.info("monitor: 下载器无任务")
            metric_state["free_seed_active"].set(0)
            return MonitorResult(
                scanned=0,
                remaining_active_downloads=0,
                summary="下载器无任务",
                steps=(
                    JobStep("查询默认下载器", "ok", 1, default_dl.name),
                    JobStep("拉取下载器任务", "skip", 0, "下载器无任务"),
                ),
            )

        # 3) 查 torrent_index 中由 qB 任务对应的 pushed 行
        # 关键：先有 qB 任务（权威），再按 qB hash 查 db 状态。
        # 这样 qB 已删/从未推的种不会被纳入统计；qB 中所有任务的当前状态都能查到。
        from app.services.runtime_params import get_runtime_params

        rp = get_runtime_params()
        self._pre_pause = timedelta(
            minutes=rp.get_int("monitor_pre_pause_minutes")
        )
        threshold = format_bj(bj_now_dt() + self._pre_pause)
        stalled_timeout_min = rp.get_int("monitor_stalled_timeout_minutes")
        min_dlspeed_kbps = rp.get_int("monitor_min_dlspeed_kbps")
        max_actions = rp.get_int("monitor_max_actions_per_run")
        now_ts = time.time()

        # 统计默认下载器内全部在途下载任务（含手动推送，共享带宽与槽位）
        initial_active_hashes: set[str] = {
            (info.hash or "").lower()
            for info in infos
            if info.hash and is_active_download(info)
        }
        active_without_hash = sum(
            1 for info in infos if not info.hash and is_active_download(info)
        )

        qb_hashes: set[str] = {
            (info.hash or "").lower()
            for info in infos
            if info.hash
        }
        if not qb_hashes:
            logger.info("monitor: 下载器无任务 hash")
            metric_state["free_seed_active"].set(active_without_hash)
            return MonitorResult(
                scanned=scanned,
                remaining_active_downloads=active_without_hash,
                summary="下载器任务无 hash",
                steps=(
                    JobStep("查询默认下载器", "ok", 1, default_dl.name),
                    JobStep("拉取下载器任务", "ok", scanned, f"{scanned} 个"),
                    JobStep("匹配 torrent_index", "skip", 0, "下载器任务无 hash"),
                ),
            )

        # 分批 in_：SQLite 变量上限约 999/32766，任务数一多整条 SQL 直接报
        # "too many SQL variables"，monitor 会永久失败（窗口看护停摆）
        hash_list = sorted(qb_hashes)
        rows: list[Any] = []
        for i in range(0, len(hash_list), 900):
            batch = hash_list[i : i + 900]
            async with session_scope() as session:
                rows += list(
                    (
                        await session.execute(
                            select(
                                TorrentIndex.info_hash,
                                TorrentIndex.free_type,
                                TorrentIndex.free_end_at,
                            ).where(
                                TorrentIndex.info_hash.in_(batch),
                                TorrentIndex.push_status == PushStatus.PUSHED,
                                # 不按 free_type 过滤：monitor 看种子当前实际状态
                                # - 当前是限时种（TIMED/TWOU_TIMED）→ 应用窗口规则
                                # - 当前是永久种（PERMANENT）→ 永不暂停
                                # - 当前是折扣/未知（neutral_leech/unknown）→ 立即暂停
                                # 站点状态变化由 fetch_all_sites 正常更新 db，无需历史快照。
                            )
                        )
                    ).all()
                )
        index_by_hash: dict[str, Any] = {
            (row.info_hash or "").lower(): row
            for row in rows
            if row.info_hash
        }

        # 4) 遍历下载器任务，按 hash 匹配 + 按 db 当前 free_type 决定行为
        matched = 0
        skipped_unmatched = 0
        skipped_far = 0
        skipped_completed = 0
        skipped_paused = 0
        skipped_checking = 0
        skipped_permanent = 0
        skipped_refund = 0
        aborted_fatal = 0
        aborted_stalled = 0
        skipped_action_cap = 0
        pre_paused = 0
        deleted = 0
        failed = 0
        errors: list[str] = []
        handled_hashes: set[str] = set()
        # 逐种明细（前端「步骤卡片」展开后可见）——回答"动了哪些种子"
        action_items: list[JobStepItem] = []

        # 动作模式（0=仅暂停 / 1=删任务+删数据），作业开始时读一次——
        # 一次巡检内保持同一动作，避免中途改参数导致同一批种子一半暂停
        # 一半删除。
        delete_mode = rp.get_int("monitor_delete_expired") == 1

        async def _apply(reason: str, *, trigger: str) -> bool:
            """对当前种子执行动作（暂停 / 删除），并记录明细、审计日志与指标。

            闭包：直接改外层的 ``deleted`` / ``pre_paused`` / ``failed`` /
            ``errors`` / ``action_items``（Python 的 nonlocal 语义）。
            返回 True 表示动作成功（该种子已脱离在途下载集合）。
            """
            nonlocal deleted, pre_paused, failed
            elapsed_min = (
                int(now_ts - info.added_at) // 60
                if info.added_at is not None
                else None
            )
            action, err = await self._act(
                adapter,
                info.hash or "",
                delete_mode,
                downloader_name=default_dl.name,
                name=info.name or "",
                reason=reason,
                completed_bytes=info.completed_bytes,
                size=info.size,
                dlspeed=info.dlspeed,
                elapsed_min=elapsed_min,
            )
            label = (info.name or hash_key)[:80]
            if err is not None:
                failed += 1
                errors.append(f"hash={hash_key} {action}: {err}")
                action_items.append(
                    JobStepItem(label, "fail", None, f"{reason}：{err}")
                )
                free_seed_outcomes.labels(
                    outcome="failed", reason="action_error"
                ).inc()
                return False
            handled_hashes.add(hash_key)
            metric_action = "delete" if action == "deleted" else "pause"
            cleanup_actions.labels(action=metric_action, trigger=trigger).inc()
            free_seed_outcomes.labels(outcome="aborted", reason=trigger).inc()
            if action == "deleted":
                deleted += 1
                action_items.append(
                    JobStepItem(label, "ok", None, f"{reason} → 已删除（含磁盘数据）")
                )
            else:
                pre_paused += 1
                action_items.append(
                    JobStepItem(label, "ok", None, f"{reason} → 已暂停")
                )
            return True

        def _hit_action_cap(reason: str) -> bool:
            """检查是否达到单轮异常/停滞清理上限（防网络抖动批量误清）。"""
            nonlocal skipped_action_cap
            if max_actions <= 0 or (aborted_fatal + aborted_stalled) < max_actions:
                return False
            skipped_action_cap += 1
            free_seed_outcomes.labels(
                outcome="skipped", reason="action_cap"
            ).inc()
            label = (info.name or hash_key)[:80]
            action_items.append(
                JobStepItem(
                    label,
                    "warn",
                    None,
                    f"{reason} → 已达单轮清理上限（{max_actions}），延至下轮",
                )
            )
            logger.warning(
                "monitor 达到单轮异常/停滞清理上限（%d），跳过本轮处理："
                "downloader=%s hash=%s name=%r reason=%s",
                max_actions,
                default_dl.name,
                hash_key,
                info.name or "",
                reason,
            )
            return True

        for info in infos:
            hash_key = (info.hash or "").lower()
            row = index_by_hash.get(hash_key)
            if row is None:
                # qB 任务但 db 没 tracked：可能是用户手动推、auto-push 之外的
                # category、或 push_status 没成功写入（极少见）。
                skipped_unmatched += 1
                continue
            matched += 1

            if is_completed(info):
                skipped_completed += 1
                continue
            if info.state.is_paused:
                skipped_paused += 1
                continue
            if info.state == TorrentState.CHECKING:
                # 正在校验（recheck）——两种模式都跳过：
                # 1) 暂停模式：等校验完成后由下一轮巡检处理（作业每 10 分钟一轮）
                # 2) 删除模式：**绝不删除**校验中的种子——校验期 progress 是
                #    「校验进度」不是完成度，判定不可靠，而删磁盘数据不可逆
                #    （AGENTS.md：删磁盘数据是净损失）。
                skipped_checking += 1
                continue

            # 1) 致命错误（ERROR / MISSING_FILES）——不受免费类型限制，立即清理
            if info.state.is_fatal:
                if _hit_action_cap("下载异常（错误/文件丢失）"):
                    continue
                if await _apply("下载异常（错误/文件丢失）", trigger="fatal"):
                    aborted_fatal += 1
                continue

            # 按 db 当前 free_type 决定（fetch_all_sites 已更新为最新值）
            ft = row.free_type
            fea = row.free_end_at

            if ft == FreeType.PERMANENT:
                # 永久免费：正常下载永不因窗口处理；但长时间停滞/龟速死种仍需清理腾出槽位
                stalled_reason = _stalled_or_slow_reason(
                    info,
                    now_ts=now_ts,
                    timeout_minutes=stalled_timeout_min,
                    min_dlspeed_kbps=min_dlspeed_kbps,
                )
                if stalled_reason is not None:
                    if _hit_action_cap(stalled_reason):
                        continue
                    if await _apply(stalled_reason, trigger="stalled"):
                        aborted_stalled += 1
                    continue
                skipped_permanent += 1
                continue

            if ft == FreeType.REFUND:
                # 退还种（springsunday "1d 退还"）：上传者已撤种
                # 用户规则"当前非免费种直接处理"——与 NEUTRAL/UNKNOWN 走相同路径
                # 单独计数便于统计退还种规模
                skipped_refund += 1
                await _apply("退还种", trigger="refund")
                continue

            if ft in (FreeType.TIMED, FreeType.TWOU_TIMED):
                # 限时种（含 2X 限时）：窗口判断
                if fea is None:
                    # 异常：限时种无 end_at → 保守处理
                    await _apply("限时种无截止时间", trigger="window_expired")
                    continue
                if fea <= threshold:
                    # 即将到期 / 已过期 → 处理
                    await _apply("临近窗口截止", trigger="window_expired")
                    continue
                # 窗口尚远：再检查是否停滞/龟速堵塞
                stalled_reason = _stalled_or_slow_reason(
                    info,
                    now_ts=now_ts,
                    timeout_minutes=stalled_timeout_min,
                    min_dlspeed_kbps=min_dlspeed_kbps,
                )
                if stalled_reason is not None:
                    if _hit_action_cap(stalled_reason):
                        continue
                    if await _apply(stalled_reason, trigger="stalled"):
                        aborted_stalled += 1
                    continue
                skipped_far += 1
                continue

            # 其他 free_type（neutral_leech / unknown / 任何非免费）→ 直接处理
            # 用户规则："当前不是免费种，直接处理"
            await _apply("已非免费种", trigger="non_free")

        initial_active = len(initial_active_hashes) + active_without_hash
        remaining_active = len(initial_active_hashes - handled_hashes) + active_without_hash
        metric_state["free_seed_active"].set(remaining_active)
        action_items, action_items_total = build_items(action_items)
        result = MonitorResult(
            scanned=scanned,
            matched=matched,
            skipped_unmatched=skipped_unmatched,
            skipped_far_from_window=skipped_far,
            skipped_already_completed=skipped_completed,
            skipped_already_paused=skipped_paused,
            skipped_checking=skipped_checking,
            skipped_permanent=skipped_permanent,
            skipped_refund=skipped_refund,
            aborted_fatal=aborted_fatal,
            aborted_stalled=aborted_stalled,
            skipped_action_cap=skipped_action_cap,
            remaining_active_downloads=remaining_active,
            pre_paused=pre_paused,
            deleted=deleted,
            failed=failed,
            errors=tuple(errors[:10]),
            summary=(
                (
                    f"扫描 {scanned}，命中 {matched}，删除 {deleted}（含磁盘数据）"
                    if delete_mode
                    else f"扫描 {scanned}，命中 {matched}，暂停 {pre_paused}"
                )
                + (
                    f"（异常 {aborted_fatal}，停滞 {aborted_stalled}）"
                    if aborted_fatal or aborted_stalled
                    else ""
                )
                + (
                    f"，达清理上限跳过 {skipped_action_cap}"
                    if skipped_action_cap
                    else ""
                )
                + (f"，{failed} 失败" if failed else "")
            ),
            steps=(
                JobStep("查询默认下载器", "ok", 1, default_dl.name),
                JobStep(
                    "拉取下载器任务",
                    "ok",
                    scanned,
                    f"{scanned} 个（在途 {initial_active}）",
                ),
                JobStep(
                    "匹配 torrent_index",
                    "ok",
                    matched,
                    f"{matched} 命中，{skipped_unmatched} 无关",
                ),
                JobStep(
                    "跳过已完成/暂停",
                    "ok",
                    skipped_completed + skipped_paused,
                    f"已完成 {skipped_completed}，已暂停 {skipped_paused}，"
                    f"校验中 {skipped_checking}",
                ),
                JobStep(
                    "判断窗口与异常",
                    "warn" if skipped_action_cap else "ok",
                    pre_paused + deleted,
                    f"距截止 < {int(self._pre_pause.total_seconds() // 60)}min"
                    + (
                        f" / 停滞超 {stalled_timeout_min}min"
                        if stalled_timeout_min > 0
                        else ""
                    )
                    + (
                        f"，命中异常 {aborted_fatal}、停滞 {aborted_stalled}"
                        f"，剩余在途 {remaining_active}"
                    )
                    + (
                        f"，达单轮上限跳过 {skipped_action_cap}"
                        if skipped_action_cap
                        else ""
                    ),
                ),
                JobStep(
                    "自动删除" if delete_mode else "自动暂停",
                    "warn" if (failed or skipped_action_cap) else "ok",
                    deleted if delete_mode else pre_paused,
                    (
                        f"已删除 {deleted} 个（含磁盘数据），{failed} 失败"
                        if delete_mode
                        else f"已暂停 {pre_paused} 个，{failed} 失败"
                    )
                    + (
                        f"，上限跳过 {skipped_action_cap}"
                        if skipped_action_cap
                        else ""
                    ),
                    items=action_items,
                    items_total=action_items_total,
                ),
            ),
        )
        logger.info(
            "monitor: 下载器 %d，命中 %d，触发%s %d（异常 %d / 停滞 %d / 上限跳过 %d），"
            "剩余在途 %d，跳过(无关/远/已完成/已暂停/校验中/永久/退还) "
            "%d/%d/%d/%d/%d/%d/%d，失败 %d",
            result.scanned, result.matched,
            "删除" if delete_mode else "暂停",
            result.deleted if delete_mode else result.pre_paused,
            result.aborted_fatal, result.aborted_stalled, result.skipped_action_cap,
            remaining_active,
            result.skipped_unmatched, result.skipped_far_from_window,
            result.skipped_already_completed, result.skipped_already_paused,
            result.skipped_checking,
            result.skipped_permanent, result.skipped_refund,
            result.failed,
        )
        return result
