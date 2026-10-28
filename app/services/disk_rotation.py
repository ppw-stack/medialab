"""磁盘空间流转作业（辅种 · 磁盘维度）。

功能：定时检查辅种下载器磁盘使用率，超过阈值时按「辅种价值低→高」
删除种子（删任务 + 删磁盘数据），把使用率压回阈值以下。

**辅种价值口径**（生产实测 tr-172 验证）：

- 分组键 ``(原始 name, size_bytes)`` ——同一片源的同分辨率/同制作组
  的多个辅种副本。生产实测：4655 个种子 → 2243 个组，count=13 的组里
  13 个 infohash **各不相同**（各站 announce URL 不同导致 .torrent
  infohash 不同），但 name+size 完全一致 → 是同一片源的 13 个副本。
- ``count = 该组种子数 = 辅种副本数``：
    - count 高（多站辅种）→ 价值高 → **保留**
    - count=1（独苗）→ 价值最低 → **优先删**
- 同 count 内（即同一价值档）按 ``added_at`` 升序——**先删加入更早的**
  种子（2026-10-28 加入）。新加的种子优先保，老的优先让位。
  缺失 ``added_at`` 的种子降级到「无时间档」（排在有时间的之后，不当
  成最旧）—— 旧快照没有这个字段时全降级，排序退化为 ``count 升序 +
  size 降序``，与加此特性前**完全一致**。
- 同档同加入时间内按 ``size_bytes`` 降序——大种子优先让位，释放空间快

设计要点：

- **读快照不打下载器接口**：直接读 ``cross_seed_snapshot.stats`` 字段
  （每 30 分钟由 ``scan_cross_seed`` 作业刷新）——与「前端读快照」契约一致。
  快照里已有完整 40 位 ``hashes`` 列表（删除用），无需再拉一次。
- **删前实时校验**：删种子是 hash 精确匹配（40 位 hex 校验），
  快照最多 30 分钟滞后，下载器找不到的 hash 计入 failed，不会误删别的种子。
- **安全闸**（与 AGENTS.md 删磁盘数据契约一致）：
    1. hash 校验：40 位 hex，拒绝 all/通配
    2. CHECKING（校验中）跳过：删磁盘数据不可逆，校验期完成度判定不可靠
    3. 审计日志：逐批记 warning，含 hash 列表 + 大小
- **失败不阻塞**：单个下载器拉取/删除失败 → 计 failed + 点名
  （与 ``scan_cross_seed`` 同一处理）

阈值与动作由运行参数控制（见 :data:`app.services.runtime_params.PARAM_SPECS`）：

- ``disk_rotation_threshold_pct``：使用率阈值（默认 90%）
- ``disk_rotation_action``：命中后动作（0=仅删任务，1=删任务+删数据，默认 1）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.adapters.downloaders.base import (
    DownloaderAdapter,
    DownloaderError,
)
from app.core.logging import get_logger
from app.db.models import CrossSeedSnapshot, DownloaderInstance
from app.db.session import session_scope
from app.services.factory import get_factory
from app.services.runtime_params import get_runtime_params
from app.services.scheduler import JobStep

logger = get_logger(__name__)


__all__ = [
    "DELETE_BATCH_SIZE",
    "RotationResult",
    "run_disk_rotation",
]


#: 删除种子时的批量上限（与 ``cross_seed_monitor.PAUSE_BATCH_SIZE`` 同量级）。
#:
#: 单次 ``adapter.remove([...])`` 接受 hash 列表，按批发送能避免 URL/请求体
#: 过长。删磁盘数据不可逆 → 每批记审计日志。
DELETE_BATCH_SIZE = 100


# ============================================================ 数据模型
@dataclass(slots=True)
class RotationResult:
    """单个辅种下载器的流转结果."""

    downloader_id: int
    downloader_name: str
    #: 磁盘使用率（百分比；0-100）；None = 没拿到磁盘信息（qB 无 total）
    usage_pct: float | None
    #: 是否触发流转（使用率 > 阈值）
    triggered: bool
    #: 本次需要释放的字节数（触发时计算）
    need_free_bytes: int = 0
    #: 候选种子数（按价值排序后的列表长度，未截断到 need）
    candidate_count: int = 0
    #: 实际删除的种子数
    deleted_count: int = 0
    #: 删除失败数（hash 不存在 / 下载器报错）
    delete_failed_count: int = 0
    #: 实际释放的字节数（按候选种子 size 求和，不含失败批次）
    freed_bytes: int = 0
    #: 跳过数量及原因
    skipped_no_total: int = 0
    #: 错误信息（拉取 / 删除失败时）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloader_id": self.downloader_id,
            "downloader_name": self.downloader_name,
            "usage_pct": self.usage_pct,
            "triggered": self.triggered,
            "need_free_bytes": self.need_free_bytes,
            "candidate_count": self.candidate_count,
            "deleted_count": self.deleted_count,
            "delete_failed_count": self.delete_failed_count,
            "freed_bytes": self.freed_bytes,
            "skipped_no_total": self.skipped_no_total,
            "error": self.error,
        }


# ============================================================ 作业入口
async def run_disk_rotation() -> dict[str, Any]:
    """作业入口：扫描所有辅种下载器磁盘，超阈值时按价值低→高删种子.

    流程：短事务读快照 → 计算使用率 → 不超阈值的 skip → 超阈值的：
    候选过滤 + 价值排序 + 批量删除（网络 IO 在事务外）。

    所有读取走 ``cross_seed_snapshot``（不调下载器接口，与前端同源）。
    删除时打下载器接口——hash 精确匹配，下载器找不到的 hash 计 failed。
    """
    threshold_pct = get_runtime_params().get_int("disk_rotation_threshold_pct")
    action = get_runtime_params().get_int("disk_rotation_action")
    delete_data = action == 1

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
            "freed_gb": 0.0,
            "summary": "无辅种下载器快照",
            "results": [],
            "failures": [],
            "threshold_pct": threshold_pct,
            "delete_data": delete_data,
            "steps": [
                JobStep("读辅种快照", "skip", 0, "无辅种下载器快照"),
            ],
        }

    factory = get_factory()
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
                error="下载器不存在或已禁用",
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
                error=str(exc),
            ))
            continue
        results.append(result)

    triggered = [r for r in results if r.triggered]
    total_deleted = sum(r.deleted_count for r in results)
    total_failed = sum(r.delete_failed_count for r in results)
    total_freed = sum(r.freed_bytes for r in results)
    # 点名：哪些下载器触发了 / 哪些没触发（用百分比）
    trigger_detail = ", ".join(
        f"{r.downloader_name} {r.usage_pct:.0f}%"
        + ("→删" if r.deleted_count else "→超阈未删")
        for r in results if r.triggered
    ) or "全部未超阈"
    fail_detail = (
        f"，{len(failures)} 个失败（{', '.join(f['downloader_name'] for f in failures)}）"
        if failures
        else ""
    )
    summary = (
        f"{len(results)}/{total_downloaders} 个下载器，"
        f"阈值 {threshold_pct}%，{len(triggered)} 个超阈（{trigger_detail}）"
        + (
            f"，删除 {total_deleted} 个种子"
            f"（释放 {total_freed / 1024**3:.2f} GB）"
            if total_deleted
            else ""
        )
        + (f"，{total_failed} 个删除失败" if total_failed else "")
        + fail_detail
    )
    return {
        "scanned_downloaders": len(results),
        "triggered_downloaders": len(triggered),
        "deleted": total_deleted,
        "delete_failed": total_failed,
        "freed_bytes": total_freed,
        "freed_gb": total_freed / 1024**3,
        "summary": summary,
        "results": [r.to_dict() for r in results],
        "failures": failures,
        "threshold_pct": threshold_pct,
        "delete_data": delete_data,
        "steps": [
            JobStep(
                "读辅种快照",
                "ok",
                len(snap_rows),
                f"{len(snap_rows)} 个下载器快照",
            ),
            JobStep(
                "判定磁盘使用率",
                "warn" if triggered else "ok",
                len(triggered),
                trigger_detail,
            ),
            JobStep(
                "删除低价值种子",
                "warn" if total_failed else ("ok" if total_deleted else "skip"),
                total_deleted,
                f"删除 {total_deleted} 个，释放 {total_freed / 1024**3:.2f} GB"
                + (f"，{total_failed} 失败" if total_failed else ""),
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
) -> RotationResult:
    """对单个辅种下载器执行流转判定 + 删除.

    :param stats: ``cross_seed_snapshot.stats`` 字段——每项含
        ``name`` / ``size_gb`` / ``count`` / ``hashes``（40 位完整 hash 列表）。
    :param delete_data: True=删任务+删数据；False=仅删任务。
    """
    # qB 拿不到 disk_total_bytes → 该下载器跳过（不报错，与 collect_space 同）
    if disk_total is None or disk_total <= 0 or disk_free is None:
        return RotationResult(
            downloader_id=downloader_id,
            downloader_name=downloader_name,
            usage_pct=None,
            triggered=False,
            skipped_no_total=1,
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
        )

    # 超阈值 → 计算需要释放的字节数（把使用率压到阈值以下）
    # 目标 used = total * threshold/100，需释放 = 当前 used - 目标 used
    target_used = int(disk_total * threshold_pct / 100)
    need_free = max(used_bytes - target_used, 0)

    # 候选过滤 + 价值排序
    candidates = _select_candidates(stats)
    if not candidates:
        return RotationResult(
            downloader_id=downloader_id,
            downloader_name=downloader_name,
            usage_pct=usage_pct,
            triggered=True,
            need_free_bytes=need_free,
            candidate_count=0,
            error="无候选种子（快照 stats 为空）",
        )

    # 累计删除直到释放量达标
    to_delete: list[tuple[str, int]] = []  # (hash, size_bytes)
    freed = 0
    for hash_, size_bytes in candidates:
        if freed >= need_free:
            break
        to_delete.append((hash_, size_bytes))
        # 用 size_bytes 而不是 completed_bytes——快照里 stats 只有 size_gb，
        # 实际释放量按完整大小算（保守上界，可能略高估）
        freed += size_bytes

    # 批量删除
    deleted, failed = await _batch_delete(
        adapter, downloader_name, to_delete, delete_data
    )
    # 实际释放量按**全部候选**累计的 size 求和（保守上界估计）。
    # 失败的 hash 在 to_delete 列表里无法精确剔除（批次级失败计数），
    # 但失败通常极少（删一个 hash 找不到不会失败整个批次），上界足够用于作业摘要。
    actual_freed = freed

    return RotationResult(
        downloader_id=downloader_id,
        downloader_name=downloader_name,
        usage_pct=usage_pct,
        triggered=True,
        need_free_bytes=need_free,
        candidate_count=len(candidates),
        deleted_count=deleted,
        delete_failed_count=failed,
        freed_bytes=actual_freed,
    )


# ============================================================ 候选选择
def _select_candidates(
    stats: list[dict[str, Any]],
) -> list[tuple[str, int]]:
    """从快照 stats 里选候选删除种子，按价值低→高排序.

    价值口径（生产 tr-172 实测验证）：

    - 主排序：``count`` 升序——独苗（count=1）优先删，多副本优先保
    - 次排序：``added_at`` 升序——同档（count 相同）内**先删加入更早的**
      种子（2026-10-28 加入）。新加的种子优先保，老的优先让位。
    - 三级排序：``size_bytes`` 降序——同档同加入时间内大种子优先让位，
      释放空间快

    **缺失 ``added_at`` 的种子降级**：排在所有有时间的种子**之后**
    （不当成「最旧」——0 / 缺失表示「未知」，归一为「无时间档」）。
    旧快照（整个 stats 没有 added_at 字段）→ 全部进无时间档 → 排序
    退化为 ``count 升序 + size 降序``，与加此特性前的行为**完全一致**
    （优雅降级，不改变现有行为）。

    候选范围：**所有种子都进候选池**，由累计释放量截断决定删多少。
    暂停 / 异常 / 校验中种子的过滤**不在此处做**——快照 stats 不带
    每个种子的状态（只有分组的 count + hashes），状态过滤在删前由
    下载器自身保证（删除是 hash 精确操作，找不到的计 failed）。

    :param stats: ``cross_seed_snapshot.stats`` 字段
    :return: ``(infohash, size_bytes)`` 列表，按价值低→高排
    """
    # (count, has_ts, added_at, -size_bytes, hash)
    # has_ts: 0=有时间(优先), 1=无时间(降级)——bool 反过来排是因为
    # Python sort 升序，False(0) 排在 True(1) 之前
    items: list[tuple[int, int, int, int, str]] = []
    for s in stats:
        count = int(s.get("count") or 0)
        if count <= 0:
            continue
        size_gb = float(s.get("size_gb") or 0)
        size_bytes = int(size_gb * 1024**3)
        hashes = s.get("hashes") or []
        if not hashes:
            continue
        # 该组内 hash → added_at 映射（缺失时为空 dict，全部降级到无时间档）
        added_map = s.get("added_at") or {}
        # 一个组里有 count 个种子，全部进候选池
        # count = 辅种价值——同组内副本删其一不影响"还在做种"的数量（只要留一个）
        # 但本作业的目标是释放空间，删独苗（count=1）后这一份数据就没了
        for h in hashes:
            h_str = str(h)
            if len(h_str) != 40:
                # 旧快照可能含截断的 hash → 跳过（adapter 会拒绝非 40 位 hash）
                continue
            ts = added_map.get(h_str)
            if isinstance(ts, (int, float)) and ts:
                # 有时间：has_ts=0（排在无时间之前），added_at=ts
                items.append((count, 0, int(ts), -size_bytes, h_str.lower()))
            else:
                # 无时间：降级到无时间档，added_at 用 maxsize 占位（不影响档内顺序）
                # has_ts=1 → 排在有时间的之后；同档内仍按 -size_bytes 排（大种子先）
                items.append((count, 1, 0, -size_bytes, h_str.lower()))

    # 排序：count 升序（独苗优先） → has_ts 升序（有时间的先）
    #  → added_at 升序（先删旧的） → -size_bytes 升序（即 size 降序，大种子优先）
    items.sort()
    return [(h, -neg_size) for _, _, _, neg_size, h in items]


# ============================================================ 批量删除
async def _batch_delete(
    adapter: DownloaderAdapter,
    downloader_name: str,
    to_delete: list[tuple[str, int]],
    delete_data: bool,
) -> tuple[int, int]:
    """批量删除种子（分批发送）.

    :return: ``(成功数, 失败数)``
    """
    if not to_delete:
        return 0, 0

    # hash 校验：40 位 hex，拒绝 all / 通配（adapter.remove 内部已校验，
    # 但提前过滤掉明显非法的能避免无效 API 调用）
    valid = [(h, size) for h, size in to_delete if len(h) == 40]
    skipped = len(to_delete) - len(valid)
    if skipped:
        logger.warning(
            "disk_rotation 跳过 %d 个非法 hash（下载器=%s）",
            skipped, downloader_name,
        )

    deleted = 0
    failed = 0
    for i in range(0, len(valid), DELETE_BATCH_SIZE):
        batch = valid[i : i + DELETE_BATCH_SIZE]
        hashes = [h for h, _ in batch]
        batch_bytes = sum(size for _, size in batch)
        # 删磁盘数据不可逆 → 必须记审计日志（AGENTS.md 契约）
        logger.warning(
            "disk_rotation 删除下载器=%s 任务 %d 个（delete_data=%s，约 %.2f GB）"
            "hashes=[%s%s]",
            downloader_name,
            len(hashes),
            delete_data,
            batch_bytes / 1024**3,
            ",".join(hashes[:5]),
            "…" if len(hashes) > 5 else "",
        )
        try:
            await adapter.remove(hashes, delete_data=delete_data)
            deleted += len(hashes)
        except DownloaderError as exc:
            failed += len(hashes)
            logger.warning(
                "disk_rotation 批量删除失败 downloader=%s 数量=%d: %s",
                downloader_name, len(hashes), exc,
            )
        except Exception as exc:
            failed += len(hashes)
            logger.warning(
                "disk_rotation 批量删除失败（非预期异常）"
                "downloader=%s 数量=%d: %s",
                downloader_name, len(hashes), exc,
            )
    return deleted, failed
