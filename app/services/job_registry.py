"""调度作业注册表（把作业挂到调度中心）。

与 :mod:`app.services.jobs` 分工：
- :mod:`app.services.jobs` — 作业**业务逻辑**（站点拉取 + upsert）
- 本模块 — 作业**注册**（JobSpec 定义、cron 配置、结果序列化）

已注册作业：

- ``fetch_all_sites`` — 按站遍历所有启用站点拉取种子列表，写入 torrent_index
- ``push_free_torrents`` — 选免费种自动推到默认下载器（经 task_queue 幂等入队）
- ``monitor_free_window`` — 巡检已推免费种，临近窗口截止自动暂停
- ``refresh_user_data`` — 拉取 9 站用户数据（上传/下载/做种数）
- ``cross_seed_maintenance`` — **辅种维护**（2026-11-01 合并）：
  辅种站巡查（落快照 + 暂停异常种子）→ 磁盘空间流转 → 孤儿文件扫描，
  三段顺序执行、每 3 小时一次
- ``drain_task_queue`` — 排空任务队列：崩溃恢复（僵死 running 回收）+ 退避重试
- ``daily_checkin`` — 对支持签到的站点执行每日签到
  （hhanclub / soulvoice / hdhome / hdsky / hdwing）
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
#: - ``37 8``  : daily_checkin（每天 08:37，避开所有多 5 分钟的作业）
#: - ``17 */3``: cross_seed_maintenance（辅种维护，合并巡查/磁盘/孤儿）
DEFAULT_CRON: dict[str, str] = {
    "fetch_all_sites": "*/10 * * * *",
    "push_free_torrents": "*/30 * * * *",
    "monitor_free_window": "5,15,25,35,45,55 * * * *",
    "refresh_user_data": "*/30 * * * *",
    "drain_task_queue": "*/5 * * * *",
    "daily_checkin": "37 8 * * *",
    # 辅种维护（2026-11-01 合并 scan_cross_seed + disk_rotation + scan_orphans）：
    # 巡查 → 磁盘流转 → 孤儿扫描，三段顺序执行。
    # 分钟位 17 避开所有多 5 分钟的作业（fetch_all_sites */10、push/refresh */30、
    # monitor 5,15,…、drain */5）。
    "cross_seed_maintenance": "17 */3 * * *",
    # 媒体刮削（每天一次足够——策略默认「仅补缺」，重跑不重复下载）：
    # 分钟位 41 避开所有既有作业（*/10、*/30、5,15,…、*/5、37 8、17 */3）。
    "scrape_metadata": "41 3 * * *",
    # 下载整理（每小时——用户在等下载入库，不宜太久）：
    # 分钟位 23 避开所有既有作业（*/10、*/30、5,15,…、*/5、37 8、17 */3、41 3）。
    "organize_files": "23 * * * *",
    # 网盘聚合归拢（每小时）：副数据源的缓冲文件搬回主数据源。
    # 分钟位 29 避开所有既有作业。
    #
    # **为什么是每小时**：归拢速度受**云添加额度**限制（光鸭实测 3 次/账号/天），
    # 不是受调度频率限制——跑得再勤也不会超过当日额度。小时级足够，
    # 且撞上额度耗尽时只是「等明天」，不会白扣。
    "aggregate_sync": "29 * * * *",
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
        name="cross_seed_maintenance",
        title="辅种维护",
        description=(
            "辅种站巡检（落快照 + 暂停异常种子）→ 磁盘超阈删种子 → 孤儿文件扫描"
            "（每 3 小时）"
        ),
        make_func=lambda f: _job_fn(_run_cross_seed_maintenance),
        log_extra="（巡查 → 磁盘流转 → 孤儿扫描，三段顺序执行）",
    ),
    JobMeta(
        name="scrape_metadata",
        title="媒体刮削",
        description=(
            "扫描本地媒体库目录，为未刮削的视频补齐 NFO 与图片（TMDB）"
        ),
        make_func=lambda f: _job_fn(_run_scrape_metadata),
    ),
    JobMeta(
        name="organize_files",
        title="下载整理",
        description=(
            "扫描整理源目录里的视频，识别后按标准结构 copy/move 到本地"
            "媒体库，并触发刮削"
        ),
        make_func=lambda f: _job_fn(_run_organize),
    ),
    JobMeta(
        name="aggregate_sync",
        title="聚合归拢",
        description=(
            "把副数据源缓冲的文件搬回主数据源（云下载），"
            "主盘确认落盘后自动清理副盘副本"
        ),
        make_func=lambda f: _job_fn(_run_aggregate_sync),
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


# ============================================================ 作业模块预热


def preflight_job_imports() -> None:
    """预热全部作业模块的导入（**进程级一次性，必须在事件循环之外跑**）。

    **为什么存在（2026-11-07 prod 事故，172 QNAP）**：下方各 ``_run_*``
    作业入口使用**函数内惰性导入**——模块在进程启动时不加载，等作业第一次
    触发时才在**事件循环线程上**现场读盘加载整条模块链（push_free /
    fetch_all_sites / monitor … 各自一条）。FS 健康时每条几毫秒；当晚 QNAP
    存储池 92% 满冷缓存，第一次 cron 触发在循环线程上爬了几分钟导入 →
    事件循环反复冻结 → 面板假死 + 未提交写事务挂起 → 连锁
    ``database is locked``（job_runs 落库失败、drain_task_queue 失败）。
    与 watchfiles 的同步导入是**同一类问题**（见 ``watcher._preflight_watchdeps``）。

    在 lifespan 的 ``register_jobs()`` **之前**通过 ``asyncio.to_thread``
    调用：预热线程读盘期间循环保持响应，完成后作业才可能被调度——触发时刻
    ``sys.modules`` 全部命中，纯内存查找。

    ⚠️ 导入失败会向上抛：部署了坏代码就该在启动时炸，而不是作业触发时。
    ⚠️ 新增作业若带新的惰性导入，必须同步加进下面的模块清单——
    ``test_scheduler.py::test_preflight_covers_all_lazy_imports`` 会用 AST
    校验两者不脱节。
    """
    import importlib

    for mod in (
        "app.services.runtime_params",  # _current_pre_pause_minutes
        "app.services.jobs",            # _run_fetch_all_sites
        "app.services.push_free",       # _run_push_free
        "app.services.monitor",         # _run_monitor_free_window
        "app.services.user_data",       # _run_refresh_user_data
        "app.services.task_drain",      # _run_drain_task_queue
        "app.services.checkin",         # _run_daily_checkin
        "app.services.cross_seed_maintenance",  # _run_cross_seed_maintenance
        "app.services.scrape_jobs",     # _run_scrape_metadata（整理刮削迁移）
        "app.services.organize_service",  # _run_organize（整理刮削迁移）
        "app.services.aggregate_sync",  # _run_aggregate_sync（网盘聚合归拢）
    ):
        importlib.import_module(mod)


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


async def _run_drain_task_queue(factory: AdapterFactory) -> dict[str, object]:
    """排空任务队列（崩溃恢复 + 退避重试）。"""
    from app.services.task_drain import drain_task_queue

    return await drain_task_queue(factory)


async def _run_daily_checkin(factory: AdapterFactory) -> dict[str, object]:
    """执行站点每日签到。"""
    from app.services.checkin import refresh_all_sites

    return await refresh_all_sites(factory)


async def _run_cross_seed_maintenance() -> dict[str, object]:
    """执行辅种维护（巡查 → 磁盘流转 → 孤儿扫描，顺序执行）。"""
    from app.services.cross_seed_maintenance import run_cross_seed_maintenance

    return await run_cross_seed_maintenance()


async def _run_scrape_metadata() -> dict[str, object]:
    """执行媒体刮削（本地媒体库 → NFO + 图片）。"""
    from app.services.scrape_jobs import run_scrape_metadata

    return await run_scrape_metadata()


async def _run_organize() -> dict[str, object]:
    """执行下载整理（源目录 → 本地媒体库标准结构）。"""
    from app.services.organize_service import run_organize

    return await run_organize()


async def _run_aggregate_sync() -> dict[str, object]:
    """执行聚合归拢（副数据源 → 主数据源）。"""
    from app.services.aggregate_sync import run_aggregate_sync

    return await run_aggregate_sync()


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
