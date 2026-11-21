"""磁盘空间流转作业（辅种 · 磁盘维度）。

功能：定时检查辅种下载器磁盘使用率，超过阈值时按「辅种价值低→高」
删除种子（删任务 + 删磁盘数据），把使用率压回阈值以下。

**删除的原子单位是「数据组」，不是「种子」**（2026-11-21 修正，重要）：

辅种的本质是**同一条数据被多个种子共享**——实测辅种站 4646 个种子只对应
2237 份数据，声明占用 71.47 TB 却大于分区总量 28.33 TB。同
``(原始 name, size_bytes)`` 的一组种子指向**同一份磁盘数据**，因此：

- 删组内**一个成员**：磁盘一个字节都不释放（其余副本仍引用那份数据），
  只是白丢一个站点的做种位；
- 删数据模式（``disk_rotation_action=1``）下删一个成员：共用文件被一起删除，
  余下 N-1 个副本立刻变 ``missingFiles``，要等下一轮巡查才被暂停——留下一批
  死种子，还把「高价值组」打成半残。

故候选与删除都以**组**为单位：一次把该组**全部** hash 交给 ``remove``，
``freed_bytes`` 按组大小记**一次**。旧实现把组内每个 hash 拆成独立候选、
逐个累加 ``size_gb``，得到的是**幽灵释放量**——循环拿着假账往下走，
目标永远达不成，越删越多。

**释放量有上界**（2026-11-21）：``need_free`` 由**整个分区**的使用率算出，
而候选池只有辅种数据。分区被媒体库 / 备份占满时，辅种全删光也补不上差额。
现在先算 ``max_releasable_bytes``（所有组整组删能释放的量），取
``min(need_free, max_releasable)``；达不到就**停手**并在结果里标
``unreachable``、写清「需人工介入」，而不是把辅种删空。

**辅种价值口径**（生产 tr-172 实测验证）：

- 分组键 ``(原始 name, size_bytes)`` ——同一片源的同分辨率/同制作组的
  多个辅种副本。生产实测：4655 个种子 → 2243 个组，count=13 的组里
  13 个 infohash **各不相同**（各站 announce URL 不同导致 .torrent
  infohash 不同），但 name+size 完全一致 → 是同一片源的 13 个副本。
- ``count = 该组种子数 = 辅种副本数``：
    - count 高（多站辅种）→ 价值高 → **保留**（同一份数据，站越多收益越高）
    - count=1（独苗）→ 价值最低 → **优先删**
- 同 count 内（即同一价值档）按组内**最早**的 ``added_at`` 升序——先删
  加入更早的种子（2026-10-28 加入）。新加的种子优先保，老的优先让位。
  组内任一种子缺失 ``added_at`` 时该组降级到「无时间档」（排在有时间的
  之后）—— 旧快照没有这个字段时全降级，排序退化为 ``count 升序 +
  size 降序``，与加此特性前**完全一致**。
- 同档同加入时间内按 ``size_bytes`` 降序——大组优先让位，释放空间快

设计要点：

- **读快照不打下载器接口**：直接读 ``cross_seed_snapshot.stats`` 字段
  （每 3 小时由 ``cross_seed_maintenance`` 作业刷新）——与「前端读快照」契约一致。
  快照里已有完整 40 位 ``hashes`` 列表（删除用），无需再拉一次。
- **快照新鲜度守卫**（2026-11-21）：① 巡查对某台下载器抓取失败时**不写**
  快照（保留上一轮），② 若无条件读就会按几天前的 hash 列表执行**不可逆删除**。
  故两道闸：① 把本轮失败的下载器 id 传进 ``skip_downloader_ids``；
  ② 自查 ``scanned_at`` 年龄，超过 :data:`SNAPSHOT_MAX_AGE_HOURS` 跳过并点名。
- **删前实时校验**：删种子是 hash 精确匹配（40 位 hex 校验），
  下载器找不到的 hash 计入 failed，不会误删别的种子。
- **保护闸**（与 AGENTS.md 删磁盘数据契约一致）：
    1. hash 校验：40 位 hex，拒绝 all/通配（适配器入口再校验一次）
    2. 组完整性：组内有任何非法 hash → **整组跳过**（删一半等于破坏数据）
    3. 快照新鲜度：见上
    4. 审计日志：逐批记 warning，含 hash 列表 + 组名 + 大小
- **失败不阻塞**：单个下载器拉取/删除失败 → 计 failed + 点名
  （与辅种巡查同一处理）
- **预演模式**（``disk_rotation_dry_run=1``）：只计算「会删哪几组、能释放多少」，
  不发任何删除指令——首次调阈值 / 改动作前先跑一轮。

阈值与动作由运行参数控制（见 :data:`app.services.runtime_params.PARAM_SPECS`）：

- ``disk_rotation_threshold_pct``：使用率阈值（默认 90%）
- ``disk_rotation_action``：命中后动作（0=仅删任务，1=删任务+删数据，默认 1）
- ``disk_rotation_dry_run``：1=仅预演（默认 0）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.adapters.downloaders.base import (
    DownloaderAdapter,
    DownloaderError,
)
from app.core.logging import get_logger
from app.core.timeutils import bj_now_dt, parse_bj
from app.db.models import CrossSeedSnapshot, DownloaderInstance
from app.db.session import session_scope
from app.services.factory import get_factory
from app.services.runtime_params import get_runtime_params
from app.services.scheduler import JobStep

logger = get_logger(__name__)


__all__ = [
    "DELETE_BATCH_SIZE",
    "SNAPSHOT_MAX_AGE_HOURS",
    "CandidateGroup",
    "RotationResult",
    "run_disk_rotation",
]


#: 删除种子时的批量上限（与 ``cross_seed_monitor.PAUSE_BATCH_SIZE`` 同量级）。
#:
#: 单次 ``adapter.remove([...])`` 接受 hash 列表，按批发送能避免 URL/请求体
#: 过长。删磁盘数据不可逆 → 每批记审计日志。
#:
#: **批次不切开一个组**：组被拆到两批里，第一批成功第二批失败就留下「半个组」
#: ——磁盘数据已删、部分副本还在做种（必然 ``missingFiles``）。故按**组**装箱，
#: 单组超过上限时独占一批。
DELETE_BATCH_SIZE = 100

#: 快照允许的最大年龄（小时）。超过 → 判定数据过期，**跳过**该下载器。
#:
#: 2 × 作业周期（``17 */3`` = 3 小时）= 6 小时：允许漏掉一两轮，
#: 但连续失败超过半天就说明 ① 已经不正常，此时按旧 hash 列表删数据
#: 是拿不可逆动作赌一个陈旧判断。
SNAPSHOT_MAX_AGE_HOURS = 6.0

#: 一次 ``set_pause`` 之外的 hash 长度常量（40 位 hex）。
_HASH_LEN = 40


# ============================================================ 数据模型
@dataclass(frozen=True, slots=True)
class CandidateGroup:
    """一个删除候选 = **一份共享数据**（同名同大小的一组种子）。

    ``hashes`` 是组内**全部**成员的 infohash——删除必须整组下发，
    只删一半既释放不了空间，也会让余下成员 ``missingFiles``。
    """

    name: str
    size_bytes: int
    count: int
    hashes: tuple[str, ...]
    #: 组内最早的加入时刻（unix 秒）；None = 组内缺时间（降级到无时间档）
    oldest_added_at: int | None = None
    #: 组内**此刻不该被删**的 hash（校验中 / 下载中，来自快照）。
    #:
    #: 非空 → 整组本轮跳过：删组是整份数据的原子操作，组内有成员正在校验
    #: 或下载时删它，既可能被下载器拒绝，也会丢半个任务（与 AGENTS
    #: 「校验中的种子绝不删除」同口径）。
    blocked_hashes: tuple[str, ...] = ()

    @property
    def deletable(self) -> bool:
        """本轮能否删这个组（组内没有校验中/下载中的成员）。"""
        return not self.blocked_hashes

    def sort_key(self) -> tuple[int, int, int, int, str]:
        """价值低→高排序键。

        count 升序（独苗优先）→ 有时间优先 → 最早的先删 → 大组先删 → 名称兜底。

        ``has_time`` 用 ``0``（有时间）/ ``1``（无时间）参与升序排序：Python
        升序排前=先删，故有时间段整体排在无时间段之前（与旧行为一致）。
        """
        has_time = 0 if self.oldest_added_at is not None else 1
        return (
            self.count,
            has_time,
            self.oldest_added_at or 0,
            -self.size_bytes,
            self.name,
        )


@dataclass(slots=True)
class RotationResult:
    """单个辅种下载器的流转结果."""

    downloader_id: int
    downloader_name: str
    #: 磁盘使用率（百分比；0-100）；None = 没拿到磁盘信息（qB 无 total）
    usage_pct: float | None
    #: 是否触发流转（使用率 > 阈值）
    triggered: bool
    #: **原始**需求：把使用率压回阈值需要释放的字节数（未按上界收敛）
    #:
    #: 与 :attr:`max_releasable_bytes` 相减 = 缺多少（> 0 即不可达）。
    #: 收敛后的实际删除量看 :attr:`planned_bytes`。
    need_free_bytes: int = 0
    #: 整组删空能释放的字节数（可释放上界）
    max_releasable_bytes: int = 0
    #: 目标是否**不可达**（候选全删也不够）→ 需人工介入
    unreachable: bool = False
    #: 候选组数（按价值排序后的组数，未截断）
    candidate_count: int = 0
    #: 因「组内有成员在校验/下载」而本轮跳过的组数
    blocked_groups: int = 0
    #: 计划删除的组数 / 种子数 / 字节（预演模式看这三个）
    planned_groups: int = 0
    planned_torrents: int = 0
    planned_bytes: int = 0
    #: 实际删除的种子数
    deleted_count: int = 0
    #: 实际删除的组数
    deleted_groups: int = 0
    #: 删除失败数（批次失败计入该批全部种子）
    delete_failed_count: int = 0
    #: 实际释放的字节数（只计**成功**删除的组，按组大小记一次）
    freed_bytes: int = 0
    #: 跳过数量及原因
    skipped_no_total: int = 0
    #: 预演模式（只算不删）
    dry_run: bool = False
    #: 错误信息（拉取 / 删除失败 / 快照过期时）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloader_id": self.downloader_id,
            "downloader_name": self.downloader_name,
            "usage_pct": self.usage_pct,
            "triggered": self.triggered,
            "need_free_bytes": self.need_free_bytes,
            "max_releasable_bytes": self.max_releasable_bytes,
            "unreachable": self.unreachable,
            "candidate_count": self.candidate_count,
            "blocked_groups": self.blocked_groups,
            "planned_groups": self.planned_groups,
            "planned_torrents": self.planned_torrents,
            "planned_bytes": self.planned_bytes,
            "deleted_count": self.deleted_count,
            "deleted_groups": self.deleted_groups,
            "delete_failed_count": self.delete_failed_count,
            "freed_bytes": self.freed_bytes,
            "skipped_no_total": self.skipped_no_total,
            "dry_run": self.dry_run,
            "error": self.error,
        }


# ============================================================ 作业入口
async def run_disk_rotation(
    *,
    skip_downloader_ids: frozenset[int] | None = None,
    max_snapshot_age_hours: float = SNAPSHOT_MAX_AGE_HOURS,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """作业入口：扫描所有辅种下载器磁盘，超阈值时按价值低→高删**组**.

    流程：短事务读快照 → 新鲜度校验 → 计算使用率 → 不超阈值的 skip →
    超阈值的：候选组过滤 + 价值排序 + 按组批量删除（网络 IO 在事务外）。

    所有读取走 ``cross_seed_snapshot``（不调下载器接口，与前端同源）。
    删除时打下载器接口——hash 精确匹配，下载器找不到的 hash 计 failed。

    :param skip_downloader_ids: 本轮**不可信**的下载器 id（① 巡查抓取失败的
        那些）——它们的快照没更新，按旧数据删数据不可逆，故跳过并点名。
    :param max_snapshot_age_hours: 快照最大年龄；超过即跳过该下载器。
    :param dry_run: ``True`` = 只算不删；``None`` = 读运行参数。
    """
    params = get_runtime_params()
    threshold_pct = params.get_int("disk_rotation_threshold_pct")
    action = params.get_int("disk_rotation_action")
    delete_data = action == 1
    if dry_run is None:
        dry_run = params.get_int("disk_rotation_dry_run") == 1
    skip_ids = skip_downloader_ids or frozenset()

    async with session_scope() as session:
        snap_rows = list(
            (
                await session.execute(
                    select(CrossSeedSnapshot).order_by(
                        CrossSeedSnapshot.downloader_id.asc()
                    )
                )
            ).scalars().all()
        )
        # 同时读辅种下载器配置（删除时需要拿连接信息）
        downloader_ids = [r.downloader_id for r in snap_rows]
        dl_rows: list[DownloaderInstance] = []
        if downloader_ids:
            dl_rows = list(
                (
                    await session.execute(
                        select(DownloaderInstance).where(
                            DownloaderInstance.id.in_(downloader_ids)
                        )
                    )
                ).scalars().all()
            )
    dl_by_id = {r.id: r for r in dl_rows}

    total_downloaders = len(snap_rows)
    if not snap_rows:
        return {
            "scanned_downloaders": 0,
            "triggered_downloaders": 0,
            "deleted": 0,
            "deleted_groups": 0,
            "freed_gb": 0.0,
            "unreachable_downloaders": 0,
            "summary": "无辅种下载器快照",
            "results": [],
            "failures": [],
            "threshold_pct": threshold_pct,
            "delete_data": delete_data,
            "dry_run": dry_run,
            "steps": [
                JobStep("读辅种快照", "skip", 0, "无辅种下载器快照"),
            ],
        }

    factory = get_factory()
    now = bj_now_dt()
    results: list[RotationResult] = []
    failures: list[dict[str, Any]] = []
    for snap in snap_rows:
        dl = dl_by_id.get(snap.downloader_id)
        if dl is None or not dl.enabled:
            results.append(RotationResult(
                downloader_id=snap.downloader_id,
                downloader_name=snap.downloader_name,
                usage_pct=None,
                triggered=False,
                skipped_no_total=1,
                dry_run=dry_run,
                error="下载器不存在或已禁用",
            ))
            continue

        # 闸 1：本轮巡查失败的下载器（快照没刷新）→ 不碰
        if snap.downloader_id in skip_ids:
            results.append(RotationResult(
                downloader_id=snap.downloader_id,
                downloader_name=snap.downloader_name,
                usage_pct=None,
                triggered=False,
                skipped_no_total=1,
                dry_run=dry_run,
                error="本轮巡查抓取失败（快照未更新），跳过删除",
            ))
            continue

        # 闸 2：快照年龄（连 ① 也没成功过 → 宁可不动）
        stale_reason = _stale_reason(snap.scanned_at, now, max_snapshot_age_hours)
        if stale_reason:
            logger.warning(
                "辅种下载器 %s 的快照过期（%s），本轮不删种子",
                snap.downloader_name, stale_reason,
            )
            results.append(RotationResult(
                downloader_id=snap.downloader_id,
                downloader_name=snap.downloader_name,
                usage_pct=None,
                triggered=False,
                skipped_no_total=1,
                dry_run=dry_run,
                error=stale_reason,
            ))
            continue

        try:
            result = await _rotate_one(
                factory.downloader(dl),
                snap.downloader_id,
                snap.downloader_name,
                snap.disk_free_bytes,
                snap.disk_total_bytes,
                list(snap.stats or []),
                threshold_pct,
                delete_data,
                dry_run,
            )
        except Exception as exc:
            logger.warning(
                "辅种下载器 %s (id=%d) 流转失败: %s",
                snap.downloader_name, snap.downloader_id, exc,
            )
            failures.append({
                "downloader_id": snap.downloader_id,
                "downloader_name": snap.downloader_name,
                "error": str(exc),
            })
            results.append(RotationResult(
                downloader_id=snap.downloader_id,
                downloader_name=snap.downloader_name,
                usage_pct=None,
                triggered=False,
                dry_run=dry_run,
                error=str(exc),
            ))
            continue
        results.append(result)

    triggered = [r for r in results if r.triggered]
    unreachable = [r for r in results if r.unreachable]
    total_deleted = sum(r.deleted_count for r in results)
    total_groups = sum(r.deleted_groups for r in results)
    total_failed = sum(r.delete_failed_count for r in results)
    total_freed = sum(r.freed_bytes for r in results)
    total_planned_groups = sum(r.planned_groups for r in results)
    total_planned_torrents = sum(r.planned_torrents for r in results)
    total_planned_bytes = sum(r.planned_bytes for r in results)
    total_blocked_groups = sum(r.blocked_groups for r in results)
    # 点名：哪些下载器触发了 / 哪些没触发（用百分比）
    trigger_detail = ", ".join(
        f"{r.downloader_name} {r.usage_pct:.0f}%"
        + (
            f"→预演删 {r.planned_groups} 组"
            if r.dry_run and r.planned_groups
            else ("→删" if r.deleted_count else "→超阈未删")
        )
        for r in results if r.triggered
    ) or "全部未超阈"
    fail_detail = (
        f"，{len(failures)} 个失败（{', '.join(f['downloader_name'] for f in failures)}）"
        if failures
        else ""
    )
    unreachable_detail = (
        "，目标不可达（"
        + "、".join(
            f"{r.downloader_name} 缺 "
            f"{(r.need_free_bytes - r.max_releasable_bytes) / 1024**3:.2f} GB"
            # 有冻结组时缺口可能是**临时**的（下轮解冻就能补上）——
            # 不说清这一点会让人以为必须手工介入
            + (
                f"，其中 {r.blocked_groups} 组本轮冻结"
                if r.blocked_groups
                else ""
            )
            for r in unreachable
        )
        + ("）" if all(r.blocked_groups for r in unreachable) else "，需人工介入）")
        if unreachable
        else ""
    )
    skipped_untrusted = [
        r for r in results if r.error and "巡查抓取失败" in r.error
    ]
    skipped_stale = [
        r for r in results
        if r.error and r.error.startswith(("快照已过期", "快照时间不可解析"))
    ]
    skip_detail = ""
    if skipped_untrusted or skipped_stale:
        bits: list[str] = []
        if skipped_untrusted:
            bits.append(f"{len(skipped_untrusted)} 个本轮巡查失败")
        if skipped_stale:
            bits.append(f"{len(skipped_stale)} 个快照过期")
        skip_detail = "，跳过删除：" + "、".join(bits)

    if dry_run:
        action_text = (
            f"[预演] 计划删除 {total_planned_groups} 组 / {total_planned_torrents} 个种子，"
            f"约 {total_planned_bytes / 1024**3:.2f} GB（未执行）"
            if total_planned_groups
            else "[预演] 无需删除"
        )
    else:
        action_text = (
            f"删除 {total_groups} 组 / {total_deleted} 个种子"
            f"（释放 {total_freed / 1024**3:.2f} GB）"
            if total_deleted
            else "无删除"
        )
    summary = (
        f"{len(results)}/{total_downloaders} 个下载器，"
        f"阈值 {threshold_pct}%，{len(triggered)} 个超阈（{trigger_detail}），"
        + action_text
        + (f"，{total_failed} 个删除失败" if total_failed else "")
        + unreachable_detail
        + skip_detail
        + (
            f"，{total_blocked_groups} 个组在校验/下载（本轮跳过）"
            if total_blocked_groups
            else ""
        )
        + fail_detail
    )
    return {
        "scanned_downloaders": len(results),
        "triggered_downloaders": len(triggered),
        "unreachable_downloaders": len(unreachable),
        "deleted": total_deleted,
        "deleted_groups": total_groups,
        "delete_failed": total_failed,
        "freed_bytes": total_freed,
        "freed_gb": total_freed / 1024**3,
        "planned_groups": total_planned_groups,
        "planned_torrents": total_planned_torrents,
        "planned_bytes": total_planned_bytes,
        "blocked_groups": total_blocked_groups,
        "summary": summary,
        "results": [r.to_dict() for r in results],
        "failures": failures,
        "threshold_pct": threshold_pct,
        "delete_data": delete_data,
        "dry_run": dry_run,
        "steps": [
            JobStep(
                "读辅种快照",
                "warn" if (skipped_untrusted or skipped_stale) else "ok",
                len(snap_rows),
                f"{len(snap_rows)} 个下载器快照" + skip_detail,
            ),
            JobStep(
                "判定磁盘使用率",
                "warn" if triggered else "ok",
                len(triggered),
                trigger_detail,
            ),
            JobStep(
                "预演删除计划" if dry_run else "删除低价值种子组",
                (
                    "skip" if not (total_planned_groups or total_deleted)
                    else ("warn" if total_failed or unreachable else "ok")
                ),
                total_planned_groups if dry_run else total_groups,
                action_text
                + (f"，{total_failed} 失败" if total_failed else "")
                + (
                    f"，{total_blocked_groups} 组校验/下载中（跳过）"
                    if total_blocked_groups
                    else ""
                ),
            ),
        ],
    }


# ============================================================ 单下载器流转
async def _rotate_one(
    adapter: DownloaderAdapter,
    downloader_id: int,
    downloader_name: str,
    disk_free: int | None,
    disk_total: int | None,
    stats: list[dict[str, Any]],
    threshold_pct: int,
    delete_data: bool,
    dry_run: bool,
) -> RotationResult:
    """对单个辅种下载器执行流转判定 + 按**组**删除.

    :param stats: ``cross_seed_snapshot.stats`` 字段——每项含
        ``name`` / ``size_gb`` / ``size_bytes``（新）/ ``count`` /
        ``hashes``（40 位完整 hash 列表）/ ``added_at``。
    :param delete_data: True=删任务+删数据；False=仅删任务。
    :param dry_run: True=只算不删。
    """
    # qB 拿不到 disk_total_bytes → 该下载器跳过（不报错，与 collect_space 同）
    if disk_total is None or disk_total <= 0 or disk_free is None:
        return RotationResult(
            downloader_id=downloader_id,
            downloader_name=downloader_name,
            usage_pct=None,
            triggered=False,
            skipped_no_total=1,
            dry_run=dry_run,
            error=None,  # qB 不提供 total，不是故障
        )

    used_bytes = disk_total - disk_free
    usage_pct = used_bytes / disk_total * 100
    if usage_pct <= threshold_pct:
        return RotationResult(
            downloader_id=downloader_id,
            downloader_name=downloader_name,
            usage_pct=usage_pct,
            triggered=False,
            dry_run=dry_run,
        )

    # 超阈值 → 计算需要释放的字节数（把使用率压到阈值以下）
    # 目标 used = total * threshold/100，需释放 = 当前 used - 目标 used
    target_used = int(disk_total * threshold_pct / 100)
    need_free_raw = max(used_bytes - target_used, 0)

    # 候选**组**过滤 + 价值排序
    groups = _select_groups(stats, downloader_name)
    # 组内有成员在校验/下载 → 本轮整组跳过（删组是整份数据的原子操作）
    blocked_groups = [g for g in groups if not g.deletable]
    groups = [g for g in groups if g.deletable]
    if blocked_groups:
        logger.warning(
            "disk_rotation 下载器=%s 跳过 %d 个组（组内有成员在 校验/下载）: %s",
            downloader_name,
            len(blocked_groups),
            "、".join(g.name for g in blocked_groups[:5])
            + ("…" if len(blocked_groups) > 5 else ""),
        )
    if not groups:
        # 区分两种「没候选」：真的没有可用组 vs 全被校验/下载冻结
        reason = (
            f"{len(blocked_groups)} 个候选组全部在校验/下载（本轮跳过，下轮再试）"
            if blocked_groups
            else "无候选种子（快照 stats 为空或没有完整 hash）"
        )
        return RotationResult(
            downloader_id=downloader_id,
            downloader_name=downloader_name,
            usage_pct=usage_pct,
            triggered=True,
            need_free_bytes=need_free_raw,
            blocked_groups=len(blocked_groups),
            dry_run=dry_run,
            error=reason,
        )

    # 可释放上界：只有**整组删空**才真的释放那份共享数据
    max_releasable = sum(g.size_bytes for g in groups)
    need_free = min(need_free_raw, max_releasable)
    unreachable = need_free_raw > max_releasable

    # 按价值低→高挑组，直到累计释放量达标（不可达时全给）。
    # **整组粒度会"过冲"最多一组**：一组可能是 10 GB，而缺口只有 1 GB——
    # 这是刻意取舍：留半个组比多释放几 GB 糟得多（半个组 = 数据没了、任务还在）。
    planned: list[CandidateGroup] = []
    planned_bytes = 0
    for group in groups:
        if planned_bytes >= need_free:
            break
        planned.append(group)
        planned_bytes += group.size_bytes

    planned_torrents = sum(g.count for g in planned)
    if unreachable:
        logger.warning(
            "辅种下载器 %s 目标不可达：需释放 %.2f GB，辅种全部删光也只有 %.2f GB"
            "（差额需人工介入，分区里还有非辅种数据）",
            downloader_name,
            need_free_raw / 1024**3,
            max_releasable / 1024**3,
        )

    if dry_run:
        _log_plan(downloader_name, planned, planned_bytes, need_free_raw, delete_data)
        return RotationResult(
            downloader_id=downloader_id,
            downloader_name=downloader_name,
            usage_pct=usage_pct,
            triggered=True,
            need_free_bytes=need_free_raw,
            max_releasable_bytes=max_releasable,
            unreachable=unreachable,
            candidate_count=len(groups),
            blocked_groups=len(blocked_groups),
            planned_groups=len(planned),
            planned_torrents=planned_torrents,
            planned_bytes=planned_bytes,
            dry_run=True,
        )

    deleted, deleted_groups, failed, freed = await _batch_delete_groups(
        adapter, downloader_name, planned, delete_data
    )

    return RotationResult(
        downloader_id=downloader_id,
        downloader_name=downloader_name,
        usage_pct=usage_pct,
        triggered=True,
        need_free_bytes=need_free_raw,
        max_releasable_bytes=max_releasable,
        unreachable=unreachable,
        candidate_count=len(groups),
        blocked_groups=len(blocked_groups),
        planned_groups=len(planned),
        planned_torrents=planned_torrents,
        planned_bytes=planned_bytes,
        deleted_count=deleted,
        deleted_groups=deleted_groups,
        delete_failed_count=failed,
        freed_bytes=freed,
        dry_run=False,
    )


def _log_plan(
    downloader_name: str,
    planned: list[CandidateGroup],
    planned_bytes: int,
    need_free: int,
    delete_data: bool,
) -> None:
    """预演模式：把计划写进日志（不删任何东西）。"""
    logger.warning(
        "[预演] 辅种下载器=%s 计划删除 %d 组（%d 个种子），预计释放 %.2f GB"
        "（需求 %.2f GB，delete_data=%s）: %s",
        downloader_name,
        len(planned),
        sum(g.count for g in planned),
        planned_bytes / 1024**3,
        need_free / 1024**3,
        delete_data,
        "、".join(f"{g.name}(x{g.count})" for g in planned[:5])
        + ("…" if len(planned) > 5 else ""),
    )


# ============================================================ 候选选择
def _select_groups(
    stats: list[dict[str, Any]], downloader_name: str = "?"
) -> list[CandidateGroup]:
    """从快照 stats 里选出**整组**候选，按价值低→高排序.

    价值口径（生产 tr-172 实测验证）：

    - 主排序：``count`` 升序——独苗（count=1）优先删，多副本优先保
    - 次排序：``added_at`` 升序——同档（count 相同）内**先删加入更早的**
      组（2026-10-28 加入）。新加的种子优先保，老的优先让位。
    - 三级排序：``size_bytes`` 降序——同档同加入时间内大组优先让位，
      释放空间快

    **整组候选**（2026-11-21 修正）：同 ``(name, size)`` 的多个 infohash
    共用**同一份磁盘数据**，只删其中几个既释放不了空间，删数据模式下还会
    让余下副本 ``missingFiles``。故每个组返回**全部** hash，由调用方一次下发。

    **组内 hash 必须全合法**：任一 hash 非 40 位 hex（旧快照的截断值）→
    整组跳过并告警。删一半等于把数据删了还留着任务。

    :param stats: ``cross_seed_snapshot.stats`` 字段
    :param downloader_name: 仅用于告警点名
    :return: :class:`CandidateGroup` 列表，按价值低→高排
    """
    groups: list[CandidateGroup] = []
    skipped_bad: list[str] = []

    for s in stats:
        count = int(s.get("count") or 0)
        if count <= 0:
            continue
        raw_hashes = s.get("hashes") or []
        if not raw_hashes:
            # 旧快照只有 12 位 sample_hash → 删不了（适配器会拒绝）
            continue
        hashes: list[str] = []
        bad = False
        for h in raw_hashes:
            h_str = str(h)
            if len(h_str) != _HASH_LEN:
                bad = True
                break
            hashes.append(h_str.lower())
        name = str(s.get("name") or "?")
        if bad or not hashes:
            skipped_bad.append(name)
            continue

        # 尺寸：优先 `size_bytes`（新快照，精确）；旧快照回退 `size_gb`（两位小数）
        size_bytes = int(s.get("size_bytes") or 0)
        if size_bytes <= 0:
            size_bytes = int(float(s.get("size_gb") or 0) * 1024**3)
        if size_bytes <= 0:
            # 尺寸未知 → **不进候选**：算不出释放量，删了就是盲删（会把候选删光
            # 仍觉得没达标）。宁可这轮少删，也不拿数据赌一个空值。
            logger.warning(
                "disk_rotation 跳过组 %s（%s）：快照缺 size_bytes / size_gb",
                name, downloader_name,
            )
            continue

        # 组内最早加入时刻（缺时间的成员不参与；全缺 → 无时间档）
        added_map = s.get("added_at") or {}
        stamps = [
            int(v) for k, v in added_map.items()
            if isinstance(v, (int, float)) and v
        ]
        blocked = tuple(
            str(h).lower() for h in (s.get("blocked_hashes") or [])
        )
        groups.append(CandidateGroup(
            name=name,
            size_bytes=size_bytes,
            count=count,
            hashes=tuple(hashes),
            oldest_added_at=min(stamps) if stamps else None,
            blocked_hashes=blocked,
        ))

    if skipped_bad:
        logger.warning(
            "disk_rotation 跳过 %d 个组（组内含非 40 位 hash，%s）: %s",
            len(skipped_bad),
            downloader_name,
            "、".join(skipped_bad[:5]) + ("…" if len(skipped_bad) > 5 else ""),
        )

    groups.sort(key=lambda g: g.sort_key())
    return groups


# ============================================================ 批量删除
def _pack_batches(
    groups: list[CandidateGroup], limit: int
) -> list[list[CandidateGroup]]:
    """把候选组装箱成批次，**绝不切开一个组**.

    组被拆到两批里，第一批成功、第二批失败就留下「半个组」——磁盘数据已删、
    部分副本还在做种（必然 ``missingFiles``）。故单组超过 ``limit`` 时
    独占一批（宁可请求体略大，也不留半组）。
    """
    batches: list[list[CandidateGroup]] = []
    current: list[CandidateGroup] = []
    current_hashes = 0
    for group in groups:
        if current and current_hashes + len(group.hashes) > limit:
            batches.append(current)
            current = []
            current_hashes = 0
        current.append(group)
        current_hashes += len(group.hashes)
    if current:
        batches.append(current)
    return batches


async def _batch_delete_groups(
    adapter: DownloaderAdapter,
    downloader_name: str,
    groups: list[CandidateGroup],
    delete_data: bool,
) -> tuple[int, int, int, int]:
    """按**组**批量删除种子（分批发送，组不被切开）.

    :return: ``(成功种子数, 成功组数, 失败种子数, 实际释放字节数)``

        释放量只计**成功**批次、且按**组大小记一次**——组内多个副本共用
        同一份数据，逐成员累加会翻倍虚报。
    """
    if not groups:
        return 0, 0, 0, 0

    deleted = 0
    deleted_groups = 0
    failed = 0
    freed = 0
    for batch in _pack_batches(groups, DELETE_BATCH_SIZE):
        hashes = [h for g in batch for h in g.hashes]
        batch_bytes = sum(g.size_bytes for g in batch)
        batch_torrents = sum(len(g.hashes) for g in batch)
        # 删磁盘数据不可逆 → 必须记审计日志（AGENTS.md 契约）
        logger.warning(
            "disk_rotation 删除下载器=%s %d 组 / %d 个任务"
            "（delete_data=%s，约 %.2f GB）hashes=[%s%s]",
            downloader_name,
            len(batch),
            len(hashes),
            delete_data,
            batch_bytes / 1024**3,
            ",".join(hashes[:5]),
            "…" if len(hashes) > 5 else "",
        )
        try:
            await adapter.remove(hashes, delete_data=delete_data)
        except DownloaderError as exc:
            failed += batch_torrents
            logger.warning(
                "disk_rotation 批量删除失败 downloader=%s 数量=%d: %s",
                downloader_name, len(hashes), exc,
            )
            continue
        except Exception as exc:
            failed += batch_torrents
            logger.warning(
                "disk_rotation 批量删除失败（非预期异常）"
                "downloader=%s 数量=%d: %s",
                downloader_name, len(hashes), exc,
            )
            continue
        deleted += batch_torrents
        deleted_groups += len(batch)
        freed += batch_bytes
    return deleted, deleted_groups, failed, freed


# ============================================================ 新鲜度
def _stale_reason(
    scanned_at: str | None, now: datetime, max_age_hours: float
) -> str | None:
    """快照是否过期；过期返回原因（供点名），否则 None.

    ``scanned_at`` 解析不出时按**过期**处理：删除不可逆，无法确认新鲜度
    时宁可不动（fail-safe），并在结果里写清原因。
    """
    parsed = parse_bj(scanned_at)
    if parsed is None:
        return f"快照时间不可解析（scanned_at={scanned_at!r}），跳过删除"
    age_hours = (now - parsed).total_seconds() / 3600
    if age_hours > max_age_hours:
        return (
            f"快照已过期 {age_hours:.1f} 小时（上限 {max_age_hours:.0f} 小时），"
            "跳过删除"
        )
    return None
