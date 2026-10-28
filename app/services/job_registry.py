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
- ``scan_orphans`` — 扫描辅种下载器的孤儿文件（磁盘有、任务没了）
- ``disk_rotation`` — 磁盘使用率超阈时按价值低→高删辅种种子
  （独苗优先删，多副本保留；同档内先删加入更早的）
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
    # 磁盘空间流转：每 2 小时一次——磁盘不会瞬间涨满，2 小时检查一次足够。
    # 分钟位 17 避开所有多 5 分钟的作业（fetch_all_sites */10、push/refresh */30、
    # monitor 5,15,…、drain */5）与 scan_cross_seed（2,32）。
    "disk_rotation": "17 */2 * * *",
}


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


@dataclass(frozen=True, slots=True)
class JobMeta:
    """作业的展示元数据 + 执行入口。

    **中文名与描述的唯��真源**：以前前端硬编码了一份 job → 中文名的
    映射表（调度页 + 仪表盘各一份），每加一个作业就要手改前端，漏一处
    就在调度页显示原始 job 名（如 ``scan_orphans``）和 "—" 描述。

    改为由后端随作业一起下发（:meth:`SchedulerCenter.list_jobs`），
    新增作业只需在这张表里加一行。
    """

    name: str
    #: 中文名（页面标题）
    title: str
    #: 一句话说明（页面标题下方）
    description: str
    #: 作业执行入口工厂：接收 :class:`AdapterFactory`（作业自己按需建
    #: adapter），返回一个**无参可调用**（调度器按零参调用它）。
    make_func: Callable[[AdapterFactory], Callable[[], Awaitable[object]]]
    #: 追加到注册日志末尾的补充说明（如参数回显）
    log_extra: str = ""


#: 全部作业的元数据（**新增作业只需在这里加一行**）。
#:
#: cron 在 :data:`DEFAULT_CRON`（独立常量，便于测试断言覆盖）。
JOB_META: tuple[JobMeta, ...] = (
    JobMeta(
        name="fetch_all_sites",
        title="站点拉取",
        description=(
            "遍历所有启用站点拉取种子列表（API/HTML），写入 torrent_index 表"
        ),
        make_func=lambda f: _job_fn(lambda: _run_fetch_all_sites(f)),
    ),
    JobMeta(
        name="push_free_torrents",
        title="免费种推送",
        description="从 torrent_index 选免费种 → 默认下载器（每 30 分钟）",
        make_func=lambda f: _job_fn(lambda: _run_push_free(f)),
    ),
    JobMeta(
        name="monitor_free_window",
        title="免费窗口监测",
        description=(
            "巡检已推免费种，临近 free_end_at 自动暂停下载器（pre_pause=10 min）"
        ),
        make_func=lambda f: _job_fn(lambda: _run_monitor_free_window(f)),
        log_extra=f" (pre_pause={_current_pre_pause_minutes()}min)",
    ),
    JobMeta(
        name="refresh_user_data",
        title="站点信息获取",
        description=(
            "拉取 9 站上传/下载/做种数/下载中数 + 时魔（每小时站点积分）"
            "→ 写入 site_user_data 表"
        ),
        make_func=lambda f: _job_fn(lambda: _run_refresh_user_data(f)),
    ),
    JobMeta(
        name="scan_cross_seed",
        title="辅种巡查",
        description=(
            "定时抓取 is_cross_seed 下载器 → 落库快照，"
            "检测 tracker 未注册/数据丢失 → 自动暂停（每 30 分钟）"
        ),
        make_func=lambda f: _job_fn(_run_scan_cross_seed),
        log_extra="（定时抓取 + 落库快照）",
    ),
    JobMeta(
        name="drain_task_queue",
        title="任务队列排空",
        description="回收崩溃残留的僵死任务 + 重试退避到期的推送任务（每 5 分钟）",
        make_func=lambda f: _job_fn(lambda: _run_drain_task_queue(f)),
    ),
    JobMeta(
        name="daily_checkin",
        title="每日签到",
        description=(
            "对支持签到的站点自动完成每日签到"
            "（hhanclub / soulvoice / hdhome / hdsky）"
        ),
        make_func=lambda f: _job_fn(lambda: _run_daily_checkin(f)),
    ),
    JobMeta(
        name="scan_orphans",
        title="辅种孤儿文件扫描",
        description=(
            "扫描辅种下载目录，找出「磁盘有文件但任务已删」的孤儿数据（每天一次）"
        ),
        make_func=lambda f: _job_fn(_run_scan_orphans),
    ),
    JobMeta(
        name="disk_rotation",
        title="磁盘空间流转",
        description=(
            "辅种磁盘使用率超阈时，按价值低→高删种子"
            "（独苗优先删，多副本保留），同档内先删加入更早的，"
            "把使用率压回阈值以下"
        ),
        make_func=lambda f: _job_fn(_run_disk_rotation),
        log_extra="（按价值低→高删种子，同档先删旧的，把使用率压回阈值以下）",
    ),
)


def register_jobs(factory: AdapterFactory) -> None:
    """注册全部周期性作业。

    :meth:`register_jobs` 接收 :class:`AdapterFactory` 而不是具体
    adapter——作业函数本身负责按站/按下载器创建 adapter。这是与之前
    :func:`register_seed_jobs`（接受 engine+hunter 实例）最大的区别。

    cron 用 :data:`DEFAULT_CRON` 里的代码默认值（DB 里的覆盖值由
    :meth:`SchedulerCenter.apply_overrides` 在注册**之前**灌入）。
    """
    for meta in JOB_META:
        _register(meta, factory)


def _register(meta: JobMeta, factory: AdapterFactory) -> None:
    """注册单个作业并记录**实际生效**的 cron。

    日志必须打**生效值**（DB 覆盖后的），不是 :data:`DEFAULT_CRON` 里的
    默认值——打默认值会在用户改过 cron 后误导（"日志说 2,32 跑，实际
    15,45 跑"）。
    """
    scheduler_center.register(
        JobSpec(
            name=meta.name,
            func=meta.make_func(factory),
            cron=DEFAULT_CRON[meta.name],
            # 中文名 + 描述随作业一起下发——前端不再硬编码映射表
            title=meta.title,
            description=meta.description,
        )
    )
    effective = scheduler_center.list_job_cron(meta.name)
    logger.info("已注册作业：%s cron=%r%s", meta.title, effective, meta.log_extra)


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


# ============================================================ 作业入口
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


async def _run_disk_rotation() -> dict[str, object]:
    """执行磁盘空间流转（按价值低→高删种子，压回使用率阈值以下）。"""
    from app.services.disk_rotation import run_disk_rotation

    return await run_disk_rotation()


def _job_fn(
    coro_factory: Callable[[], Awaitable[object]],
) -> Callable[[], Awaitable[object]]:
    """把作业函数包成调度器的零参调用约定。

    作业函数返回 ``Awaitable``，但调度器按**零参**调用——故这里包一层
    lambda，把「调函数」和「await 结果」分开：coroutine 在**被调用时**
    才创建（不是注册时），否则注册阶段就创建了未 await 的 coroutine，
    会被 Python 警告「coroutine was never awaited」。
    """

    async def _run() -> object:
        return await coro_factory()

    return _run
