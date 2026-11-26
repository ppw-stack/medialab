"""免费种推送 + 窗口监测（2026-11-22 合并）。

**为什么合并**：这两件事本来是**同一条流水线的前后两段**——
推送把新免费种塞进下载器，监测盯着已推种子的窗口、临期暂停。
分两个作业时它们的节奏被钉死在各自的 cron 上：

============================ ==========================================
旧作业                        节奏
============================ ==========================================
``push_free_torrents``        cron 兜底 2 小时 + 上游触发，门槛 30 分钟
``monitor_free_window``       每 10 分钟（``5,15,…,55``）
============================ ==========================================

两个节奏没法用一个 cron 表达，于是页面上一堆节点、DB 里两行配置、
用户要改「免费种相关」的行为得在两个作业之间跳。

合并后是**一个作业、一个 cron 心跳（10 分钟）**，内部：

1. **免费窗口监测**（每轮都跑）——临期/非免费的暂停，属于安全动作，
   不能被节流；
2. **免费种推送**（**距上次真正推送 ≥30 分钟才跑**）——节流是必须的：
   站点下载有限频（``push_download_interval_ms``），推送每 10 分钟一轮
   只会把限频摊薄、没有收益。门槛常量 :data:`PUSH_MIN_INTERVAL_SEC`
   与旧方案里 ``JobSpec.min_trigger_interval_sec`` 是同一个值，行为连续。

**顺序为什么是「先监测后推送」**：

- 监测会把临期种子暂停，**先腾出下载器位置**再塞新的；
- 推送的选种条件本来就有 ``push_min_remaining_hours`` 过滤，先推后监测
  也不会推错，只是白跑一轮监测——反过来更省。

**节流的状态是进程内的**（``_THROTTLE`` 实例）：重启后第一次跑会立刻推送
一次。这是刻意的（新进程尽快恢复推送），且推送链路幂等（``task_queue``
的 ``idem_key`` + 索引行的 ``push_status``），不会重推已推过的种。

**只统计「真的尝试过推送」的轮次**：``selected == 0``（没有候选）或
「未配置默认下载器」这类**空转**不刷新节流时间戳——否则用户刚配好下载器
却要白等 30 分钟才推。空转的代价只是一次 DB 查询，可以每轮都试。

两段**各自容错**：一段失败不影响另一段，失败信息进 ``steps``
（前端步骤卡片逐段展示）。
"""

from __future__ import annotations

import time
from typing import Any

from app.core.logging import get_logger
from app.core.timeutils import bj_now_dt, parse_bj
from app.db.models import Site
from app.db.session import session_scope
from app.services import job_run_service
from app.services.job_compose import build_summary, run_stage, to_payload
from app.services.scheduler import JobStep

logger = get_logger(__name__)

__all__ = [
    "PUSH_MIN_INTERVAL_SEC",
    "get_free_seed_observability",
    "reset_throttle_state",
    "run_push_and_monitor",
]

#: 两次**真正推送**之间的最小间隔（秒）。
#:
#: 30 分钟是与旧 `push_free_torrents` 方案对齐的值：那时它靠
#: ``JobSpec.min_trigger_interval_sec=1800`` 限流，频率≈2 次/小时。
#: 合并后 cron 心跳是 10 分钟（监测需要），推送仍受这个门槛约束。
PUSH_MIN_INTERVAL_SEC = 1800

class _PushThrottle:
    """进程内的推送节流状态。

    状态收在实例里——模块级只留一个**不可替换**的实例，避免 ``global``
    改写模块级可变状态（测试之间互相污染、并发时行为不可预测）。
    """

    def __init__(self) -> None:
        #: 上一次**真正尝试推送**的单调时钟时刻（``None`` = 本进程还没推过）。
        self.last_push_at: float | None = None

    def reset(self) -> None:
        """清空节流时间戳（测试用；生产不需要调用）。"""
        self.last_push_at = None

    def remaining(self, now: float) -> float | None:
        """距下次可推送还剩的秒数；``None`` = 可立即推送。"""
        if self.last_push_at is None:
            return None
        remain = PUSH_MIN_INTERVAL_SEC - (now - self.last_push_at)
        return remain if remain > 0 else None

    def mark_pushed(self, now: float) -> None:
        """记录一次**真正尝试过推送**的时刻。"""
        self.last_push_at = now


#: 进程内唯一的节流状态（重启后为空 → 首轮立刻推一次，刻意）。
_THROTTLE = _PushThrottle()


def reset_throttle_state() -> None:
    """重置节流状态（测试用；生产不需要调用）。"""
    _THROTTLE.reset()


async def run_push_and_monitor(factory: Any) -> dict[str, Any]:
    """合并作业入口：① 窗口监测（每轮）→ ② 免费种推送（门槛内）。

    :param factory: :class:`~app.services.factory.AdapterFactory`——两个
        子服务都要自己按需建 adapter。
    """
    steps: list[JobStep] = []
    results: dict[str, Any] = {}

    # ① 免费窗口监测（安全动作：临期先暂停，不节流）
    mon_result, mon_step = await run_stage(
        "免费窗口监测",
        lambda: _run_monitor(factory),
    )
    results["monitor"] = mon_result
    steps.append(mon_step)

    # ② 免费种推送（距上次真正推送 ≥ 门槛才跑；复用监测段统计的剩余在途数）
    raw_active = mon_result.get("remaining_active_downloads")
    remaining_active = raw_active if isinstance(raw_active, int) else None
    push_result, push_step = await run_stage(
        "免费种推送",
        lambda: _run_push(factory, active_downloads=remaining_active),
    )
    results["push"] = push_result
    steps.append(push_step)

    return {
        "summary": build_summary(results, ("monitor", "push")),
        "steps": steps,
        **results,
    }


# ---------------------------------------------------------------- 子阶段


async def _run_monitor(factory: Any) -> dict[str, Any]:
    """跑窗口监测（返回 dataclass → dict，便于落 ``result_json``）。"""
    from app.services.monitor import MonitorFreeWindowService

    result = await MonitorFreeWindowService(factory).run()
    return to_payload(result)


async def _run_push(
    factory: Any, *, active_downloads: int | None = None
) -> dict[str, Any]:
    """跑免费种推送，受 :data:`PUSH_MIN_INTERVAL_SEC` 节流。

    空转（无候选 / 未配置下载器 / 在途已满暂缓推送 / 选种全被跳过）
    **不刷新**节流时间戳，这样新候选一出现、或下载器腾出槽位，
    下一轮就能立刻补推（≤10 分钟）。

    Args:
        factory: 服务工厂，用于构造 PushFreeService。
        active_downloads: 监测阶段统计的剩余在途下载数（可为 0）。为 None
            表示未知（如监测段失败），此时不按水位裁剪，交由
            PushFreeService 自行查询下载器决定。
    """
    now = time.monotonic()
    remain = _THROTTLE.remaining(now)
    if remain is not None:
        waited = PUSH_MIN_INTERVAL_SEC - remain
        return {
            "summary": (
                f"距上次推送 {int(waited // 60)} 分钟，"
                f"未满 {PUSH_MIN_INTERVAL_SEC // 60} 分钟门槛"
                f"（还差 {int(remain // 60) + 1} 分钟）"
            ),
            "skipped_by_interval": True,
            "steps": [],
        }

    from app.services.push_free import PushFreeService

    svc = PushFreeService(factory)
    if active_downloads is not None:
        result = await svc.run(active_downloads=active_downloads)
    else:
        result = await svc.run()
    payload = to_payload(result)

    # 只有**真的尝试过**才记时间戳：空转轮次不算，避免把下一次推送推后 30 分钟
    selected = int(payload.get("selected") or 0)
    tried = selected > 0 or int(payload.get("pushed") or 0) > 0
    if tried:
        _THROTTLE.mark_pushed(time.monotonic())
    return payload


def _as_int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _as_opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def get_free_seed_observability() -> dict[str, Any]:
    """汇总免费种推送与监测的实时观测快照（供面板 5s 轮询，不打下载器网络）。

    数据来源：
    - 进程内节流器（``_THROTTLE``）
    - 内存运行时参数（``runtime_params``）
    - 最近一次 ``push_and_monitor_free`` 执行记录（``job_runs``）
    - 当前处于熔断期的启用站点（``sites.breaker_until``）
    """
    from sqlalchemy import select

    from app.services.runtime_params import get_runtime_params, lookup_spec

    def _param(key: str, fallback: int) -> int:
        try:
            return get_runtime_params().get_int(key)
        except (RuntimeError, KeyError):
            spec = lookup_spec(key)
            return spec.default if spec is not None else fallback

    remain = _THROTTLE.remaining(time.monotonic())
    throttle_remaining_sec = int(remain) if remain is not None and remain > 0 else 0

    max_active = _param("push_max_active_downloads", 10)
    max_actions = _param("monitor_max_actions_per_run", 5)
    stalled_timeout_min = _param("monitor_stalled_timeout_minutes", 60)
    min_dlspeed_kbps = _param("monitor_min_dlspeed_kbps", 50)
    delete_expired = _param("monitor_delete_expired", 0) == 1

    last_run = await job_run_service.get_latest_job_run(
        job_name="push_and_monitor_free", include_logs=False
    )

    active_downloads: int | None = None
    last_run_at: str | None = None
    last_ok: bool | None = None
    last_monitor: dict[str, Any] | None = None
    last_push: dict[str, Any] | None = None

    if last_run is not None:
        last_run_at = last_run.finished_at or last_run.started_at
        last_ok = last_run.ok
        res = last_run.result if isinstance(last_run.result, dict) else {}
        mon = res.get("monitor") if isinstance(res.get("monitor"), dict) else None
        push = res.get("push") if isinstance(res.get("push"), dict) else None

        if mon is not None:
            mon_active = _as_opt_int(mon.get("remaining_active_downloads"))
            active_downloads = mon_active
            pre_paused = _as_int(mon.get("pre_paused"))
            deleted = _as_int(mon.get("deleted"))
            aborted_fatal = _as_int(mon.get("aborted_fatal"))
            aborted_stalled = _as_int(mon.get("aborted_stalled"))
            # 窗口止损必须读**独立计数**。旧记录没有该字段（本次之前落库的
            # 行），才退回减法反推——退还种 / 非免费种也累加 pre_paused 与
            # deleted，减法会把它们算进窗口桶，使面板虚高。
            window_raw = mon.get("aborted_window")
            aborted_window = (
                window_raw
                if isinstance(window_raw, int) and not isinstance(window_raw, bool)
                else max(0, (pre_paused + deleted) - aborted_fatal - aborted_stalled)
            )
            last_monitor = {
                "scanned": _as_int(mon.get("scanned")),
                "matched": _as_int(mon.get("matched")),
                "pre_paused": pre_paused,
                "deleted": deleted,
                "aborted_window": aborted_window,
                "aborted_fatal": aborted_fatal,
                "aborted_stalled": aborted_stalled,
                "skipped_action_cap": _as_int(mon.get("skipped_action_cap")),
                "failed": _as_int(mon.get("failed")),
                "remaining_active_downloads": mon_active,
            }

        if push is not None:
            pushed_cnt = _as_int(push.get("pushed"))
            push_active = _as_opt_int(push.get("active_downloads"))
            if push_active is not None:
                active_downloads = push_active + pushed_cnt
            elif active_downloads is not None and pushed_cnt > 0:
                active_downloads = active_downloads + pushed_cnt
            last_push = {
                "selected": _as_int(push.get("selected")),
                "pushed": pushed_cnt,
                "skipped_by_capacity": bool(push.get("skipped_by_capacity")),
                "skipped_by_interval": bool(push.get("skipped_by_interval")),
                "failed": _as_int(push.get("failed")),
            }

    now_dt = bj_now_dt()
    breaker_sites: list[dict[str, Any]] = []
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Site.id, Site.name, Site.breaker_until).where(
                    Site.enabled.is_(True),
                    Site.breaker_until.is_not(None),
                )
            )
        ).all()
    for site_id, name, breaker_until in rows:
        if not breaker_until:
            continue
        until_dt = parse_bj(breaker_until)
        if until_dt is not None and until_dt > now_dt:
            breaker_sites.append(
                {
                    "site_id": site_id,
                    "name": name,
                    "breaker_until": breaker_until,
                }
            )
    breaker_sites.sort(key=lambda item: str(item["name"]))

    return {
        "active_downloads": active_downloads,
        "max_active_downloads": max_active,
        "throttle_remaining_sec": throttle_remaining_sec,
        "max_actions_per_run": max_actions,
        "stalled_timeout_minutes": stalled_timeout_min,
        "min_dlspeed_kbps": min_dlspeed_kbps,
        "delete_expired": delete_expired,
        "last_run_at": last_run_at,
        "last_ok": last_ok,
        "last_monitor": last_monitor,
        "last_push": last_push,
        "breaker_sites": breaker_sites,
    }
