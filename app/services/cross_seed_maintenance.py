"""辅种维护作业（合并 scan_cross_seed + disk_rotation + scan_orphans）。

**为什么合并**（2026-11-01）：

1. **依赖顺序**——``disk_rotation`` 读 ``cross_seed_snapshot`` 判断磁盘
   使用率，而快照由 ``scan_cross_seed`` 产出。合并前两者 cron 独立，可能
   「巡查还没跑，rotation 就跑」→ 读到**旧快照**，判错使用率。合并后
   ② 必定基于 ① 的**最新**快照。
2. **省一次全量抓取**——辅种站抓一次要 20s+（3000+ 种子）。合并前
   巡查要抓，rotation/orphan 也各自拉下载器任务列表。
3. **失败粒度变粗**——所以三段**各自 try/except**：某段失败不影响其余，
   失败信息进 ``steps``（前端「步骤卡片」逐段展示）。

**执行顺序**：

===== ==================== ==========================================
步骤  内容                说明
===== ==================== ==========================================
①    辅种巡查             抓辅种站 → 落快照 + 暂停异常种子
②    磁盘空间流转         基于①的**最新**快照判使用率 → 超阈则删种子
③    孤儿文件扫描         扫盘找孤儿（磁盘有文件但任务已删）
===== ==================== ==========================================
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.services.scheduler import JobStep, JobStepItem, build_items

logger = get_logger(__name__)

__all__ = ["run_cross_seed_maintenance"]


async def _run_scan_cross_seed() -> dict[str, Any]:
    """辅种巡查：抓辅种站 → 落快照 + 暂停异常种子。

    单独抽成模块级函数——``CrossSeedMonitorService`` 的构造需要已初始化的
    适配器工厂，测试里不便直接构造（包一层才能 monkeypatch）。
    """
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    svc = CrossSeedMonitorService()
    return await svc.run_scan_job()


async def run_cross_seed_maintenance() -> dict[str, Any]:
    """辅种维护作业入口：巡查 → 磁盘流转 → 孤儿扫描。

    三段顺序执行、各自容错。返回合成的 ``summary`` / ``steps`` +
    三段各自的原始返回（便于排查单段细节）。
    """
    from app.services.cross_seed_orphan import run_scan_orphans
    from app.services.disk_rotation import run_disk_rotation

    steps: list[JobStep] = []
    results: dict[str, Any] = {}

    # ① 辅种巡查
    scan_result, scan_step = await _run_stage(
        "辅种巡查",
        _run_scan_cross_seed,
    )
    results["cross_seed"] = scan_result
    steps.append(scan_step)

    # ② 磁盘空间流转（基于①刚落的新快照）
    rot_result, rot_step = await _run_stage(
        "磁盘空间流转",
        run_disk_rotation,
    )
    results["disk_rotation"] = rot_result
    steps.append(rot_step)

    # ③ 孤儿文件扫描
    orphan_result, orphan_step = await _run_stage(
        "孤儿文件扫描",
        run_scan_orphans,
    )
    results["scan_orphans"] = orphan_result
    steps.append(orphan_step)

    return {
        "summary": _build_summary(results),
        "steps": steps,
        **results,
    }

async def _run_stage(
    name: str,
    coro_factory: Any,
) -> tuple[dict[str, Any], JobStep]:
    """跑一个子阶段，失败折叠成 ``fail`` 步骤（不影响其它阶段）。

    Returns:
        ``(子阶段返回 dict, JobStep)``——返回 dict 在失败时为空。
    """
    try:
        raw = await coro_factory()
    except Exception as exc:
        logger.exception("辅种维护子阶段失败：%s", name)
        return {}, JobStep(name, "fail", None, f"{type(exc).__name__}: {exc}")

    if not isinstance(raw, dict):
        return {}, JobStep(name, "warn", None, str(raw)[:120])

    # 子阶段自己带 steps（如巡查有「扫描下载器 / 写入快照 / 暂停异常」）
    # ——不展开，只取它自己的 summary 作为本步描述。
    detail = str(raw.get("summary") or "")
    status = "ok"
    if raw.get("failures") or raw.get("failed_downloaders"):
        status = "warn"
    count = _extract_count(raw)
    items, items_total = build_items(_extract_items(raw))
    return raw, JobStep(
        name, status, count, detail or None,
        items=items or None, items_total=items_total,
    )


def _extract_items(raw: dict[str, Any]) -> list[JobStepItem]:
    """从子阶段返回里抽逐项明细。

    三段的返回结构不同，但失败信息都在 ``failures`` 里
    （统一的 ``{downloader_id, downloader_name, error}`` 形态）——
    故优先读它，其余按子阶段特有的键补。
    """
    items: list[JobStepItem] = []

    # ① 统一形态的失败列表（巡查 / 磁盘流转 / 孤儿扫描都有）
    for f in raw.get("failures") or []:
        if isinstance(f, dict):
            name = str(f.get("downloader_name") or f.get("name") or "?")
            err = str(f.get("error") or "失败")
        else:
            name = "?"
            err = str(f)
        items.append(JobStepItem(name, "fail", None, err))

    # ② 各子阶段特有的成功明细
    for r in raw.get("results") or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("downloader_name") or r.get("name") or "?")
        if r.get("error"):
            continue  # 失败已由上面记录
        bits: list[str] = []
        if r.get("usage_pct") is not None:
            bits.append(f"使用率 {float(r['usage_pct']):.0f}%")
        if r.get("triggered"):
            bits.append("超阈")
        if r.get("deleted_count"):
            bits.append(f"删除 {r['deleted_count']} 个")
        if r.get("orphan_count"):
            bits.append(f"孤儿 {r['orphan_count']} 个")
        if r.get("total_torrents"):
            bits.append(f"{r['total_torrents']} 个种子")
        if r.get("paused_count"):
            bits.append(f"暂停 {r['paused_count']} 个")
        if r.get("anomaly_count"):
            bits.append(f"异常 {r['anomaly_count']} 个")
        if bits:
            items.append(JobStepItem(name, "ok", None, "，".join(bits)))

    return items


def _extract_count(raw: dict[str, Any]) -> int | None:
    """从子阶段返回里取一个「涉及数量」用于步骤卡片。"""
    for key in (
        "scanned_downloaders",
        "deleted",
        "orphan_count",
        "total_torrents",
    ):
        value = raw.get(key)
        if isinstance(value, int):
            return value
    return None


def _build_summary(results: dict[str, Any]) -> str:
    """合成总览文案（三段各一句）。"""
    parts: list[str] = []

    scan = results.get("cross_seed")
    if isinstance(scan, dict) and scan.get("summary"):
        parts.append(str(scan["summary"]))

    rot = results.get("disk_rotation")
    if isinstance(rot, dict):
        deleted = rot.get("deleted")
        freed = rot.get("freed_gb")
        if isinstance(deleted, int) and deleted > 0:
            parts.append(
                f"磁盘流转删除 {deleted} 个种子"
                + (f"，释放 {float(freed):.2f} GB" if isinstance(freed, (int, float)) else "")
            )
        elif isinstance(rot.get("summary"), str) and rot["summary"]:
            parts.append(str(rot["summary"]))

    orphan = results.get("scan_orphans")
    if isinstance(orphan, dict) and orphan.get("summary"):
        parts.append(str(orphan["summary"]))

    return "；".join(parts) if parts else "无辅种下载器"
