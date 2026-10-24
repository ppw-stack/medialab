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

1. **只删正在跑的**：已完成（``progress >= 1.0``）与已暂停的任务**跳过**
   （沿用暂停路径的跳过逻辑）。已完成的种子数据已下完，删掉是净损失。
2. **保留 ``torrent_index`` 行**：删除只作用于下载器侧，索引行与
   ``push_status=pushed`` 保持不变。``push_free`` 的候选条件要求
   ``push_status IS NULL OR != pushed``，故该种**不会**被重新推送
   ——否则「推送 → 删除 → 再推送」会无限循环。
3. **删磁盘数据不可逆**：走 ``delete_data=True``，且必须记审计日志
   （AGENTS.md：删磁盘数据单独开关并记审计日志）。

不写 DB。``torrent_index`` 中已推送但不在下载器中的种子不用管
（可能 qB 自动删除 / 用户手动删，不强制标记）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.adapters.downloaders.base import DownloaderAdapter
from app.core.logging import get_logger
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.downloader_query import get_default_downloader
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep

if TYPE_CHECKING:
    from app.adapters.downloaders.base import TorrentInfo

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MonitorResult:
    """单次监测作业的运行结果。"""

    scanned: int = 0
    matched: int = 0
    skipped_unmatched: int = 0
    skipped_far_from_window: int = 0
    skipped_already_completed: int = 0
    skipped_already_paused: int = 0
    skipped_permanent: int = 0
    skipped_refund: int = 0
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
        adapter: DownloaderAdapter, hash_: str, delete_mode: bool
    ) -> tuple[str, str | None]:
        """对单个种子执行动作（暂停或删除）。

        :param adapter: 下载器适配器。
        :param hash_: 种子 infohash（40 位 hex，由适配器校验）。
        :param delete_mode: True=删任务 + 删磁盘数据；False=仅暂停。
        :return: ``(动作名, 错误信息)``。动作名为 ``"deleted"`` /
            ``"set_pause"``；错误信息为 None 表示成功，否则是异常文本
            （调用方据此计入 failed）。
        """
        if delete_mode:
            # 删磁盘数据不可逆 → 必须记审计日志（AGENTS.md：删磁盘数据
            # 单独开关并记审计日志）。审计的取证价值在于能定位"删了什么"，
            # 故记 hash（单条，无批量截断问题）。
            logger.warning(
                "monitor 删除下载器任务并删除磁盘数据：hash=%s", hash_
            )
            try:
                await adapter.remove([hash_], delete_data=True)
            except Exception as exc:
                return "remove", str(exc)
            return "deleted", None
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
            return MonitorResult(
                scanned=0,
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

        self._pre_pause = timedelta(
            minutes=get_runtime_params().get_int("monitor_pre_pause_minutes")
        )
        threshold = format_bj(bj_now_dt() + self._pre_pause)

        qb_hashes: set[str] = {
            (info.hash or "").lower()
            for info in infos
            if info.hash
        }
        if not qb_hashes:
            logger.info("monitor: 下载器无任务 hash")
            return MonitorResult(
                scanned=scanned,
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
        skipped_permanent = 0
        skipped_refund = 0
        pre_paused = 0
        deleted = 0
        failed = 0
        errors: list[str] = []

        # 动作模式（0=仅暂停 / 1=删任务+删数据），作业开始时读一次——
        # 一次巡检内保持同一动作，避免中途改参数导致同一批种子一半暂停
        # 一半删除。
        delete_mode = get_runtime_params().get_int("monitor_delete_expired") == 1

        for info in infos:
            hash_key = (info.hash or "").lower()
            row = index_by_hash.get(hash_key)
            if row is None:
                # qB 任务但 db 没 tracked：可能是用户手动推、auto-push 之外的
                # category、或 push_status 没成功写入（极少见）。
                skipped_unmatched += 1
                continue
            matched += 1

            if info.progress >= 1.0:
                skipped_completed += 1
                continue
            if info.state.is_paused:
                skipped_paused += 1
                continue

            # 按 db 当前 free_type 决定（fetch_all_sites 已更新为最新值）
            ft = row.free_type
            fea = row.free_end_at

            if ft == FreeType.PERMANENT:
                # 永久免费：用户全程做种赚积分，永不处理
                skipped_permanent += 1
                continue

            if ft == FreeType.REFUND:
                # 退还种（springsunday "1d 退还"）：上传者已撤种
                # 用户规则"当前非免费种直接处理"——与 NEUTRAL/UNKNOWN 走相同路径
                # 单独计数便于统计退还种规模
                skipped_refund += 1
                action, err = await self._act(adapter, info.hash or "", delete_mode)
                if err is not None:
                    failed += 1
                    errors.append(f"hash={hash_key} {action}: {err}")
                elif action == "deleted":
                    deleted += 1
                else:
                    pre_paused += 1
                continue

            if ft in (FreeType.TIMED, FreeType.TWOU_TIMED):
                # 限时种（含 2X 限时）：窗口判断
                if fea is None:
                    # 异常：限时种无 end_at → 保守处理
                    action, err = await self._act(adapter, info.hash or "", delete_mode)
                    if err is not None:
                        failed += 1
                        errors.append(f"hash={hash_key} {action}: {err}")
                    elif action == "deleted":
                        deleted += 1
                    else:
                        pre_paused += 1
                    continue
                if fea > threshold:
                    skipped_far += 1
                    continue
                # 即将到期 / 已过期 → 处理
                action, err = await self._act(adapter, info.hash or "", delete_mode)
                if err is not None:
                    failed += 1
                    errors.append(f"hash={hash_key} {action}: {err}")
                elif action == "deleted":
                    deleted += 1
                else:
                    pre_paused += 1
                continue

            # 其他 free_type（neutral_leech / unknown / 任何非免费）→ 直接处理
            # 用户规则："当前不是免费种，直接处理"
            action, err = await self._act(adapter, info.hash or "", delete_mode)
            if err is not None:
                failed += 1
                errors.append(f"hash={hash_key} {action}: {err}")
            elif action == "deleted":
                deleted += 1
            else:
                pre_paused += 1

        result = MonitorResult(
            scanned=scanned,
            matched=matched,
            skipped_unmatched=skipped_unmatched,
            skipped_far_from_window=skipped_far,
            skipped_already_completed=skipped_completed,
            skipped_already_paused=skipped_paused,
            skipped_permanent=skipped_permanent,
            skipped_refund=skipped_refund,
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
                + (f"，{failed} 失败" if failed else "")
            ),
            steps=(
                JobStep("查询默认下载器", "ok", 1, default_dl.name),
                JobStep("拉取下载器任务", "ok", scanned, f"{scanned} 个"),
                JobStep(
                    "匹配 torrent_index",
                    "ok",
                    matched,
                    f"{matched} 命中，{skipped_unmatched} 无关",
                ),
                JobStep(
                    "判断免费窗口",
                    "ok",
                    pre_paused + deleted,
                    f"距截止 < {int(self._pre_pause.total_seconds() // 60)}min 需处理",
                ),
                JobStep(
                    "自动删除" if delete_mode else "自动暂停",
                    "warn" if failed else "ok",
                    deleted if delete_mode else pre_paused,
                    (
                        f"已删除 {deleted} 个（含磁盘数据），{failed} 失败"
                        if delete_mode
                        else f"已暂停 {pre_paused} 个，{failed} 失败"
                    ),
                ),
            ),
        )
        logger.info(
            "monitor: 下载器 %d，命中 %d，触发%s %d，"
            "跳过(无关/远/已完成/已暂停/永久/退还) %d/%d/%d/%d/%d/%d，失败 %d",
            result.scanned, result.matched,
            "删除" if delete_mode else "暂停",
            result.deleted if delete_mode else result.pre_paused,
            result.skipped_unmatched, result.skipped_far_from_window,
            result.skipped_already_completed, result.skipped_already_paused,
            result.skipped_permanent, result.skipped_refund,
            result.failed,
        )
        return result
