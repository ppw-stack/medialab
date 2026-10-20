"""调度作业注册表（把作业挂到调度中心）。

与 :mod:`app.services.jobs` 分工：
- :mod:`app.services.jobs` — 作业**业务逻辑**（站点拉取 + upsert）
- 本模块 — 作业**注册**（JobSpec 定义、cron 配置、结果序列化）

已注册作业：

- ``fetch_all_sites`` — 按站遍历所有启用站点拉取种子列表，写入 torrent_index
- ``push_free_torrents`` — 选免费种自动推到默认下载器（经 task_queue 幂等入队）
- ``monitor_free_window`` — 巡检已推免费种，临近窗口截止自动暂停
- ``refresh_user_data`` — 拉取 9 站用户数据（上传/下载/做种数）
- ``scan_cross_seed`` — 巡查辅种下载器，暂停 tracker 未注册 / 数据丢失的种子
- ``drain_task_queue`` — 排空任务队列：崩溃恢复（僵死 running 回收）+ 退避重试
"""
from __future__ import annotations

from collections.abc import Awaitable

from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.services.factory import AdapterFactory
from app.services.scheduler import JobSpec, scheduler_center

logger = get_logger(__name__)


def register_jobs(
    factory: AdapterFactory,
    *,
    settings: AppSettings | None = None,
) -> None:
    """注册全部周期性作业。

    :meth:`register_jobs` 接收 :class:`AdapterFactory` 而不是具体
    adapter——作业函数本身负责按站/按下载器创建 adapter。这是与之前
    :func:`register_seed_jobs`（接受 engine+hunter 实例）最大的区别。
    """
    cfg = settings or get_settings()

    scheduler_center.register(
        JobSpec(
            name="fetch_all_sites",
            func=lambda: _run_and_return(_run_fetch_all_sites(factory)),
            cron=cfg.fetch_all_sites_cron,
        )
    )
    logger.info(
        "已注册站点拉取作业：fetch_all_sites cron=%r",
        cfg.fetch_all_sites_cron,
    )

    scheduler_center.register(
        JobSpec(
            name="push_free_torrents",
            func=lambda: _run_and_return(_run_push_free(factory, cfg)),
            cron=cfg.push_free_cron,
        )
    )
    logger.info(
        "已注册免费种推送作业：push_free_torrents cron=%r",
        cfg.push_free_cron,
    )

    scheduler_center.register(
        JobSpec(
            name="monitor_free_window",
            func=lambda: _run_and_return(_run_monitor_free_window(factory, cfg)),
            cron=cfg.monitor_free_window_cron,
        )
    )
    logger.info(
        "已注册免费窗口监测作业：monitor_free_window cron=%r (pre_pause=%dmin)",
        cfg.monitor_free_window_cron, _current_pre_pause_minutes(cfg),
    )

    scheduler_center.register(
        JobSpec(
            name="refresh_user_data",
            func=lambda: _run_and_return(_run_refresh_user_data(factory)),
            cron="*/30 * * * *",
        )
    )
    logger.info("已注册站点用户数据作业：refresh_user_data cron='*/30 * * * *'")

    scheduler_center.register(
        JobSpec(
            name="scan_cross_seed",
            func=lambda: _run_and_return(_run_scan_cross_seed()),
            cron=cfg.scan_cross_seed_cron,
        )
    )
    logger.info(
        "已注册辅种巡查作业：scan_cross_seed cron=%r",
        cfg.scan_cross_seed_cron,
    )

    scheduler_center.register(
        JobSpec(
            name="drain_task_queue",
            func=lambda: _run_and_return(_run_drain_task_queue(factory)),
            cron="*/5 * * * *",
        )
    )
    logger.info("已注册任务队列排空作业：drain_task_queue cron='*/5 * * * *'")

# ============================================================ 作业入口
async def _run_fetch_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """执行站点拉取。"""
    from app.services.jobs import fetch_all_sites

    return await fetch_all_sites(factory)


async def _run_push_free(factory: AdapterFactory, cfg: AppSettings) -> dict[str, object]:
    """执行推送并把结果序列化为 dict（供 API 返回）。"""
    from dataclasses import asdict

    from app.services.push_free import PushFreeService

    svc = PushFreeService(cfg, factory)
    result = await svc.run()
    return asdict(result)


async def _run_monitor_free_window(
    factory: AdapterFactory, cfg: AppSettings
) -> dict[str, object]:
    """执行免费窗口监测并把结果序列化为 dict（供 API 返回）。"""
    from dataclasses import asdict

    from app.services.monitor import MonitorFreeWindowService

    svc = MonitorFreeWindowService(factory)
    result = await svc.run()
    return asdict(result)


def _current_pre_pause_minutes(cfg: AppSettings) -> int:
    """当前生效的"提前暂停分钟数"（DB 运行时参数优先，回退到 settings）。

    作业实际用的是 runtime_params（DB）里的值，不是 settings 里的初始值——
    启动日志若打 settings 值会与实际行为不符。
    取不到时回退到 settings 的值（而不是 0），否则日志会显示
    "pre_pause=0min" 而实际生效的是 settings 里的分钟数。
    """
    from app.services.runtime_params import get_runtime_params

    try:
        return get_runtime_params().get_int("monitor_pre_pause_minutes")
    except Exception:
        return cfg.monitor_pre_pause_minutes


async def _run_refresh_user_data(factory: AdapterFactory) -> dict[str, object]:
    """执行站点用户数据刷新。"""
    from app.services.user_data import refresh_all_sites

    return await refresh_all_sites(factory)


async def _run_scan_cross_seed() -> dict[str, object]:
    """执行辅种巡查（扫描 + 暂停异常种子）。"""
    from app.services.cross_seed_monitor import CrossSeedMonitorService

    svc = CrossSeedMonitorService()
    return await svc.run_scan_job()

async def _run_drain_task_queue(factory: AdapterFactory) -> dict[str, object]:
    """排空任务队列（崩溃恢复 + 退避重试）。"""
    from app.services.task_drain import drain_task_queue

    return await drain_task_queue(factory)


async def _run_and_return(result: Awaitable[object]) -> object:
    """await 作业函数并把返回值透传出去，供 manual run 展示详情。"""
    return await result
