"""调度作业注册表（把作业挂到调度中心）。

与 :mod:`app.services.jobs` 分工：
- :mod:`app.services.jobs` — 作业**业务逻辑**（站点拉取 + upsert）
- 本模块 — 作业**注册**（JobSpec 定义、cron 配置、结果序列化）

已注册作业：

- ``fetch_all_sites`` — 按站遍历所有启用站点拉取种子列表，写入 torrent_index
- ``push_free_torrents`` — 选免费种自动推到默认下载器（经 task_queue 幂等入队）
- ``monitor_free_window`` — 巡检已推免费种，临近窗口截止自动暂停
- ``refresh_user_data`` — 拉取 9 站用户数据（上传/下载/做种数）
- ``scan_cross_seed`` — 定时抓取辅种下载器，落库快照 + 暂停 tracker 未注册 / 数据丢失的种子
- ``drain_task_queue`` — 排空任务队列：崩溃恢复（僵死 running 回收）+ 退避重试
- ``daily_checkin`` — 对支持签到的站点执行每日签到（hhanclub / soulvoice / hdhome）
"""
from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from app.core.logging import get_logger
from app.services.factory import AdapterFactory
from app.services.scheduler import JobSpec, scheduler_center

logger = get_logger(__name__)

#: 各作业的**默认** cron 表达式（5 字段：分 时 日 月 星期）。
#:
#: **代码是默认值的唯一真源**——cron 不再从 env 读（2026-10-24 起）：
#: 用户在页面「配置」弹框改的 cron 落进 ``scheduler_job_settings`` 表，
#: 启动期回填覆盖这里的默认值；DB 里没记录才用这里的值。
#:
#: 分钟位刻意错开——避免多个作业在同一分钟并发抢资源：
#:
#: - ``*/10``  : fetch_all_sites（站点拉取）
#: - ``*/30``  : push_free_torrents / refresh_user_data
#: - ``5,15,…``: monitor_free_window（错开 fetch 与 push）
#: - ``*/5``   : drain_task_queue
#: - ``2,32``  : scan_cross_seed（辅种站单次抓取 20s+，别太密）
#: - ``37 8``  : daily_checkin（每天 08:37，避开所有多 5 分钟的作业）
DEFAULT_CRON: dict[str, str] = {
    "fetch_all_sites": "*/10 * * * *",
    "push_free_torrents": "*/30 * * * *",
    "monitor_free_window": "5,15,25,35,45,55 * * * *",
    "refresh_user_data": "*/30 * * * *",
    "scan_cross_seed": "2,32 * * * *",
    "drain_task_queue": "*/5 * * * *",
    "daily_checkin": "37 8 * * *",
    # 孤儿文件扫描：辅种站全量扫盘实测 39 秒（1669 目录 / 3.6 万文件），
    # 每天一次足够——孤儿不会快速变化。分钟位 7 避开所有多 5 分钟的作业。
    "scan_orphans": "7 4 * * *",
}


def register_jobs(factory: AdapterFactory) -> None:
    """注册全部周期性作业。

    :meth:`register_jobs` 接收 :class:`AdapterFactory` 而不是具体
    adapter——作业函数本身负责按站/按下载器创建 adapter。这是与之前
    :func:`register_seed_jobs`（接受 engine+hunter 实例）最大的区别。

    cron 用 :data:`DEFAULT_CRON` 里的代码默认值（DB 里的覆盖值由
    :meth:`SchedulerCenter.apply_overrides` 在注册**之前**灌入）。
    """
    _register(
        "fetch_all_sites",
        lambda: _run_and_return(_run_fetch_all_sites(factory)),
        "站点拉取",
    )
    _register(
        "push_free_torrents",
        lambda: _run_and_return(_run_push_free(factory)),
        "免费种推送",
    )
    _register(
        "monitor_free_window",
        lambda: _run_and_return(_run_monitor_free_window(factory)),
        "免费窗口监测",
        extra=f" (pre_pause={_current_pre_pause_minutes()}min)",
    )
    _register(
        "refresh_user_data",
        lambda: _run_and_return(_run_refresh_user_data(factory)),
        "站点用户数据",
    )
    _register(
        "scan_cross_seed",
        lambda: _run_and_return(_run_scan_cross_seed()),
        "辅种巡查",
        extra="（定时抓取 + 落库快照）",
    )
    _register(
        "drain_task_queue",
        lambda: _run_and_return(_run_drain_task_queue(factory)),
        "任务队列排空",
    )
    _register(
        "daily_checkin",
        lambda: _run_and_return(_run_daily_checkin(factory)),
        "站点签到",
    )
    _register(
        "scan_orphans",
        lambda: _run_and_return(_run_scan_orphans()),
        "辅种孤儿文件扫描",
    )


def _register(name: str, func: Any, desc: str, *, extra: str = "") -> None:
    """注册单个作业并记录**实际生效**的 cron。

    日志必须打**生效值**（DB 覆盖后的），不是 :data:`DEFAULT_CRON` 里的
    默认值——打默认值会在用户改过 cron 后误导（"日志说 2,32 跑，实际
    15,45 跑"）。
    """
    scheduler_center.register(
        JobSpec(name=name, func=func, cron=DEFAULT_CRON[name])
    )
    effective = scheduler_center.list_job_cron(name)
    logger.info("已注册作业：%s cron=%r%s", desc, effective, extra)

# ============================================================ 作业入口
async def _run_fetch_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """执行站点拉取。"""
    from app.services.jobs import fetch_all_sites

    return await fetch_all_sites(factory)


async def _run_push_free(factory: AdapterFactory) -> dict[str, object]:
    """执行推送并把结果序列化为 dict（供 API 返回）。"""
    from dataclasses import asdict

    from app.services.push_free import PushFreeService

    svc = PushFreeService(factory)
    result = await svc.run()
    return asdict(result)


async def _run_monitor_free_window(
    factory: AdapterFactory,
) -> dict[str, object]:
    """执行免费窗口监测并把结果序列化为 dict（供 API 返回）。"""
    from dataclasses import asdict

    from app.services.monitor import MonitorFreeWindowService

    svc = MonitorFreeWindowService(factory)
    result = await svc.run()
    return asdict(result)


def _current_pre_pause_minutes() -> int:
    """当前生效的"提前暂停分钟数"（DB 运行时参数优先，回退到默认值）。

    作业实际用的是 runtime_params（DB）里的值，不是启动期的初始值——
    启动日志若打初始值会与实际行为不符。

    取不到时回退到 :attr:`PARAM_SPECS` 里的默认值（而不是 0），否则日志
    会显示 "pre_pause=0min" 而实际生效的是 spec 的分钟数。
    """
    from app.services.runtime_params import get_runtime_params, lookup_spec

    try:
        return get_runtime_params().get_int("monitor_pre_pause_minutes")
    except Exception:
        spec = lookup_spec("monitor_pre_pause_minutes")
        return spec.default if spec is not None else 10


async def _run_refresh_user_data(factory: AdapterFactory) -> dict[str, object]:
    """执行站点用户数据刷新。"""
    from app.services.user_data import refresh_all_sites

    return await refresh_all_sites(factory)


async def _run_scan_cross_seed() -> dict[str, object]:
    """执行辅种巡查（定时抓取 + 落库快照 + 暂停异常种子）。"""
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    svc = CrossSeedMonitorService()
    return await svc.run_scan_job()

async def _run_drain_task_queue(factory: AdapterFactory) -> dict[str, object]:
    """排空任务队列（崩溃恢复 + 退避重试）。"""
    from app.services.task_drain import drain_task_queue

    return await drain_task_queue(factory)


async def _run_daily_checkin(factory: AdapterFactory) -> dict[str, object]:
    """执行站点每日签到。"""
    from app.services.checkin import refresh_all_sites

    return await refresh_all_sites(factory)


async def _run_scan_orphans() -> dict[str, object]:
    """执行辅种孤儿文件扫描（磁盘有、任务没了）。"""
    from app.services.cross_seed_orphan import run_scan_orphans

    return await run_scan_orphans()


async def _run_and_return(result: Awaitable[object]) -> object:
    """await 作业函数并把返回值透传出去，供 manual run 展示详情。"""
    return await result
