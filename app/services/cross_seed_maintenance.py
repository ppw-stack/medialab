"""辅种维护作业（合并 scan_cross_seed + disk_rotation + scan_orphans）。

**为什么合并**（2026-11-01）：

1. **依赖顺序**——``disk_rotation`` 读 ``cross_seed_snapshot`` 判断磁盘
   使用率，而快照由 ``scan_cross_seed`` 产出。合并前两者 cron 独立，可能
   「巡查还没跑，rotation 就跑」→ 读到**旧快照**，判错使用率。合并后
   ② 必定基于 ① 的**最新**快照。
2. **失败粒度变粗**——所以三段**各自 try/except**：某段失败不影响其余，
   失败信息进 ``steps``（前端「步骤卡片」逐段展示）。

⚠️ **只有 ①② 之间存在真实依赖，③ 没有**（2026-11-21 复核）：
③ 的输入是自己的（下载器 ``files`` + 挂载盘扫盘），与 ① 的快照无关；
而它的代价（TR 全量 files 27.5 MB + CIFS 全盘 walk 39–60 s）比 ①② 重
一个量级。合并后只能取折中：① 的新鲜度被拖到 3 小时（原 30 分钟），
③ 被催到 8 次/日（原 1 次/日）。要把 ③ 拆回独立日频作业时，直接搬到
``job_registry`` 加一条 cron 即可，本模块删掉第三段。

**执行顺序**：

===== ==================== ==========================================
步骤  内容                说明
===== ==================== ==========================================
①    辅种巡查             抓辅种站 → 落快照 + 暂停异常种子
②    磁盘空间流转         基于①的**最新**快照判使用率 → 超阈则按**组**删种子
③    孤儿文件扫描         扫盘找孤儿（磁盘有文件但任务已删）
===== ==================== ==========================================

**①→② 的数据有效性交接**（2026-11-21）：合并只保证"同轮内顺序"，不保证
"快照是本轮的"——① 对某台下载器抓取失败时**不写**快照（保留上一轮），
② 若照读就会按几天前的 hash 列表执行**不可逆删除**。故 ① 的
``failures[*].downloader_id`` 会作为 ``skip_downloader_ids`` 传给 ②，
② 再自查 ``scanned_at`` 年龄（上限 6 小时）兜底。
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
    #
    # **本轮抓取失败的下载器不许参与删除**（2026-11-21）：① 失败时**不写**
    # 快照（保留上一轮），② 若照读就会按几天前的 hash 列表执行不可逆删除。
    # 顺序保证 ≠ 数据有效——所以把 ① 的失败名单传下去，让 ② 跳过并点名。
    skip_ids = frozenset(_failure_ids(scan_result))
    if skip_ids:
        rot_result, rot_step = await _run_stage(
            "磁盘空间流转",
            lambda: run_disk_rotation(skip_downloader_ids=skip_ids),
        )
    else:
        rot_result, rot_step = await _run_stage("磁盘空间流转", run_disk_rotation)
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
    # 失败口径**三种都要认**（2026-11-21 修）：
    # - ``failures``：巡查 / 磁盘流转的逐下载器失败（{downloader_id,name,error}）
    # - ``failed_downloaders``：巡查的计数
    # - ``errors``：孤儿扫描的逐条失败（「下载器名: 原因」字符串）
    # 只认前两个时，孤儿扫描全挂也会显示 ok —— 故障被伪装成正常。
    if raw.get("failures") or raw.get("failed_downloaders") or raw.get("errors"):
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

    # ① 统一形态的失败列表（巡查 / 磁盘流转）
    for f in raw.get("failures") or []:
        if isinstance(f, dict):
            name = str(f.get("downloader_name") or f.get("name") or "?")
            err = str(f.get("error") or "失败")
        else:
            name = "?"
            err = str(f)
        items.append(JobStepItem(name, "fail", None, err))

    # ①b 孤儿扫描的失败列表（``errors`` 是「下载器名: 原因」字符串）
    for e in raw.get("errors") or []:
        text = str(e)
        name, _, err = text.partition(": ")
        items.append(JobStepItem(name or "?", "fail", None, err or text))

    # ② 各子阶段特有的成功明细
    for r in raw.get("results") or []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("downloader_name") or r.get("name") or "?")
        if r.get("error"):
            # 未计入 failures 的「跳过」（快照过期 / 本轮抓取失败 / 下载器已禁用）
            # ——照样列出来，否则页面只显示「没删」，用户不知道为什么没删
            items.append(JobStepItem(name, "skip", None, str(r["error"])))
            continue
        bits: list[str] = []
        if r.get("usage_pct") is not None:
            bits.append(f"使用率 {float(r['usage_pct']):.0f}%")
        if r.get("triggered"):
            bits.append("超阈")
        if r.get("dry_run") and r.get("planned_groups"):
            bits.append(f"预演 {r['planned_groups']} 组")
        if r.get("deleted_groups"):
            bits.append(f"删除 {r['deleted_groups']} 组")
        if r.get("deleted_count"):
            bits.append(f"删除 {r['deleted_count']} 个")
        if r.get("unreachable"):
            bits.append("目标不可达")
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


def _failure_ids(raw: dict[str, Any]) -> list[int]:
    """从子阶段返回里取**失败下载器的 id**（用于下游跳过）。

    ① 巡查的 ``failures`` 是 ``{downloader_id, downloader_name, error}``
    的统一形态——② 删除依赖「快照是本轮的」，故这些 id 必须传下去。

    取不到 id 的条目（老的字符串形态）忽略：宁可少跳过，也不能误跳过
    一台正常下载器。
    """
    ids: list[int] = []
    for f in raw.get("failures") or []:
        if not isinstance(f, dict):
            continue
        value = f.get("downloader_id")
        if isinstance(value, int):
            ids.append(value)
    return ids


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
