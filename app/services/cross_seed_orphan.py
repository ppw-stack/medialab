"""辅种孤儿文件统计 service（作业 + 查询）。

与 :mod:`app.services.orphan_scanner` 的分工：

- :mod:`app.services.orphan_scanner` —— **纯算法**（扫盘 + 顶层名匹配），
  不碰 DB、不碰网络，便于单测
- 本模块 —— **编排**：拉下载器任务 → 调算法 → 落库 → 提供查询

作业 ``cross_seed_maintenance`` 的**第三段**跑（cron ``17 */3 * * *``，
辅种站全量扫盘实测 39 秒）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.adapters.downloaders.base import TorrentFileNames
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import CrossSeedOrphan, DownloaderInstance
from app.db.session import session_scope
from app.services.env_settings import get_env_setting
from app.services.factory import get_factory
from app.services.orphan_scanner import failed_scan, parse_scan_dirs, scan_orphans
from app.services.scheduler import JobStep

logger = get_logger(__name__)

__all__ = [
    "OrphanRow",
    "collect_owned_names",
    "list_orphans",
    "run_scan_orphans",
    "scan_one_downloader",
]


@dataclass(frozen=True, slots=True)
class OrphanRow:
    """孤儿统计的一行（DB 视图）。"""

    downloader_id: int
    downloader_name: str
    scanned_at: str
    orphan_count: int
    orphan_bytes: int
    total_scan_bytes: int
    total_scan_count: int
    #: 空目录孤儿数（0 文件）——残留但**不占空间**
    empty_dir_count: int = 0
    #: 逐目录的扫描统计
    scanned_dirs: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "downloader_id": self.downloader_id,
            "downloader_name": self.downloader_name,
            "scanned_at": self.scanned_at,
            "orphan_count": self.orphan_count,
            "orphan_bytes": self.orphan_bytes,
            "total_scan_bytes": self.total_scan_bytes,
            "total_scan_count": self.total_scan_count,
            "empty_dir_count": self.empty_dir_count,
            "scanned_dirs": self.scanned_dirs,
            "orphans": self.orphans,
            "error": self.error,
        }


async def list_orphans() -> list[OrphanRow]:
    """所有辅种下载器的孤儿统计（按下载器 id 升序）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(CrossSeedOrphan).order_by(CrossSeedOrphan.downloader_id)
            )
        ).scalars().all()
    return [
        OrphanRow(
            downloader_id=r.downloader_id,
            downloader_name=r.downloader_name,
            scanned_at=r.scanned_at,
            orphan_count=r.orphan_count,
            orphan_bytes=r.orphan_bytes,
            total_scan_bytes=r.total_scan_bytes,
            total_scan_count=r.total_scan_count,
            empty_dir_count=r.empty_dir_count,
            scanned_dirs=list(r.scanned_dirs or []),
            orphans=list(r.orphans or []),
            error=r.error,
        )
        for r in rows
    ]


async def _upsert(downloader_id: int, name: str, result: Any) -> None:
    """把扫描结果 upsert 到 ``cross_seed_orphan``（短事务）。"""
    payload: dict[str, Any] = {
        "downloader_name": name,
        "scanned_at": bj_now(),
        "orphan_count": result.orphan_count,
        "orphan_bytes": result.orphan_bytes,
        "total_scan_bytes": result.total_bytes,
        "total_scan_count": result.total_count,
        "empty_dir_count": result.empty_dir_count,
        "scanned_dirs": result.scanned_dirs,
        "orphans": [o.to_dict() for o in result.orphans],
        "error": result.error,
    }
    async with session_scope() as session:
        row = (
            await session.execute(
                select(CrossSeedOrphan).where(
                    CrossSeedOrphan.downloader_id == downloader_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(CrossSeedOrphan(downloader_id=downloader_id, **payload))
        else:
            for k, v in payload.items():
                setattr(row, k, v)


async def collect_owned_names() -> tuple[
    list[TorrentFileNames], dict[int, list[TorrentFileNames]], list[str]
]:
    """拉取**所有**已启用下载器的顶层名（跨下载器汇总）。

    辅种目录可能**被多个下载器共用**——实测 qB 有 6 个任务的
    ``save_path`` 就在辅种目录里。若「有主」只看辅种下载器（TR）的任务，
    这 6 个 qB 任务的数据会被误判成孤儿（实测误判 480 GB，占「孤儿」
    总量的 78%）。故必须汇总**所有**已启用下载器。

    :returns: ``(全部顶层名, {下载器 id: 该下载器的顶层名}, 失败的下载器名)``。

        中间那个字典让调用方**复用**已拉到的数据——每个下载器只拉一次
        （辅种站拉一次 20s+，拉两次代价太大）。

        失败的下载器不贡献「有主」条目 → 它们的数据可能被误判为孤儿，
        故**必须**在结果里点名（前端据此提示「判定可能不完整」）。
    """
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(DownloaderInstance)
                    .where(DownloaderInstance.enabled.is_(True))
                    .order_by(DownloaderInstance.id)
                )
            ).scalars().all()
        )

    factory = get_factory()
    all_files: list[TorrentFileNames] = []
    by_downloader: dict[int, list[TorrentFileNames]] = {}
    failed: list[str] = []
    for row in rows:
        adapter = factory.downloader(row)
        try:
            files = await adapter.list_torrent_file_names()
        except Exception as exc:
            # 拉取失败 → 该下载器的数据可能被误判成孤儿 → 记名
            logger.warning(
                "下载器 %s 拉取失败（孤儿判定不完整）：%s", row.name, exc
            )
            failed.append(row.name)
            continue
        by_downloader[row.id] = files
        all_files.extend(files)
    return all_files, by_downloader, failed


async def scan_one_downloader(
    row: DownloaderInstance,
    owned: list[TorrentFileNames] | None = None,
    own_files: list[TorrentFileNames] | None = None,
) -> dict[str, Any]:
    """扫描单个辅种下载器的孤儿文件。

    **网络 IO 在事务外**：拉任务 → 扫盘 → 短事务落库。

    拉取用 :meth:`DownloaderAdapter.list_torrent_file_names`——它给出
    每个种子的顶层名**与**它的 downloadDir（孤儿匹配按 downloadDir 分组
    所需的两样都在里面），**只打一次**下载器接口。

    为什么不用 :meth:`DownloaderAdapter.list_torrents_with_errors`：顶层名
    需要下载器的**文件列表**才能算出，而 TR 的 ``files`` 字段在辅种站是
    27.5 MB（15.8 万个文件条目）。混进巡查路径会让每次辅种巡查
    （30 分钟一次）都白拉这 27 MB——巡查并不需要文件列表。故拆成独立
    方法，只有本作业（每天一次）付这个代价。

    :param owned: **跨下载器汇总**的「有主」集合（见
        :func:`collect_owned_names`）。None = 只用本下载器的任务
        （单下载器调试用；生产路径必须传汇总值，否则共用目录的其他
        下载器的数据会被误判成孤儿）。
    :param own_files: 本下载器**已拉取**的顶层名——传了就复用（不重复
        拉取）；None = 本函数自己拉一次。
    """
    factory = get_factory()
    adapter = factory.downloader(row)
    if own_files is not None:
        files = own_files
    else:
        try:
            files = await adapter.list_torrent_file_names()
        except Exception as exc:
            logger.warning(
                "辅种下载器 %s 拉取失败（孤儿扫描跳过）：%s", row.name, exc
            )
            result = failed_scan(str(exc) or type(exc).__name__)
            await _upsert(row.id, row.name, result)
            return result.to_dict()

    scan_dirs = parse_scan_dirs(get_env_setting("cross_seed_scan_dirs"))
    # 汇总所有下载器的顶层名 + 本下载器的（汇总里已含本下载器，重复无害）
    merged = list(owned) if owned is not None else []
    merged.extend(files)
    result = scan_orphans(merged, scan_dirs)
    await _upsert(row.id, row.name, result)
    return result.to_dict()


async def run_scan_orphans() -> dict[str, Any]:
    """作业入口：扫描所有辅种下载器的孤儿文件。

    流程：短事务读下载器 → 逐个拉取+扫盘（网络/IO 在事务外）→
    短事务落库。
    """
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.is_cross_seed.is_(True),
                        DownloaderInstance.enabled.is_(True),
                    ).order_by(DownloaderInstance.id)
                )
            ).scalars().all()
        )

    if not rows:
        return {
            "scanned_downloaders": 0,
            "orphan_count": 0,
            "orphan_bytes": 0,
            "summary": "无辅种下载器",
            "steps": [JobStep("查询辅种下载器", "skip", 0, "无辅种下载器")],
        }

    # 跨下载器汇总「有主」集合——辅种目录可能被多个下载器共用
    # （实测 qB 有 6 个任务在辅种目录里）。只看辅种下载器会把那些
    # 正在做种的数据误判成孤儿（实测误判 480 GB / 78%）。
    owned, by_downloader, pull_failed = await collect_owned_names()
    if pull_failed:
        logger.warning(
            "部分下载器拉取失败，孤儿判定可能不完整：%s", "、".join(pull_failed)
        )

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        result = await scan_one_downloader(
            row,
            owned=owned,
            own_files=by_downloader.get(row.id),
        )
        results.append(result)
        if result.get("error"):
            errors.append(f"{row.name}: {result['error']}")

    orphan_count = sum(r["orphan_count"] for r in results)
    orphan_bytes = sum(r["orphan_bytes"] for r in results)
    summary = (
        f"{len(rows)} 个下载器，{orphan_count} 个孤儿文件，"
        f"{orphan_bytes / 1024**3:.2f} GB"
    )
    if errors:
        summary += f"，{len(errors)} 个扫描失败"
    if pull_failed:
        summary += f"，{len(pull_failed)} 个下载器拉取失败（判定不完整）"

    return {
        "scanned_downloaders": len(rows),
        "orphan_count": orphan_count,
        "orphan_bytes": orphan_bytes,
        "summary": summary,
        "errors": errors,
        "steps": [
            JobStep(
                "汇总各下载器的任务",
                "warn" if pull_failed else "ok",
                len(owned),
                f"{len(owned)} 个种子"
                + (
                    f"，{len(pull_failed)} 个下载器失败（{'、'.join(pull_failed)}）"
                    if pull_failed
                    else ""
                ),
            ),
            JobStep(
                "扫描辅种下载器",
                "warn" if errors else "ok",
                len(rows),
                f"{len(rows)} 个下载器"
                + (f"，{len(errors)} 个失败" if errors else ""),
            ),
            JobStep(
                "发现孤儿文件",
                "warn" if orphan_count else "ok",
                orphan_count,
                f"{orphan_count} 个，{orphan_bytes / 1024**3:.2f} GB",
            ),
        ],
    }
