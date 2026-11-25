"""调度作业注册表（把作业挂到调度中心）。

与 :mod:`app.services.jobs` 分工：
- :mod:`app.services.jobs` — 作业**业务逻辑**（站点拉取 + upsert）
- 本模块 — 作业**注册**（JobSpec 定义、cron 配置、结果序列化）

已注册作业（**按业务模块分组**——「定时任务」页据此分块展示）：

辅种域（``module="seed"``）：

- ``fetch_all_sites`` — 按站遍历所有启用站点拉取种子列表，写入 torrent_index
- ``push_and_monitor_free`` — **免费种推送 + 窗口监测**（2026-11-22 合并）：
  巡检已推免费种（临期/非免费自动暂停）＋ 把新免费种推到默认下载器
  （cron 10 分钟心跳；推送内部节流 ≥30 分钟）
- ``cross_seed_maintenance`` — **辅种维护**（2026-11-01 合并）：
  辅种站巡查（落快照 + 暂停异常种子）→ 磁盘空间流转 → 孤儿文件扫描，
  三段顺序执行、每 3 小时一次
- ``drain_task_queue`` — 排空任务队列：崩溃恢复（僵死 running 回收）+ 退避重试
  （hhanclub / soulvoice / hdhome / hdsky / hdwing）

媒体库域（``module="media"``）：

- ``scrape_metadata`` — 媒体刮削（本地目录补 NFO / 图片）
- ``organize_files`` — 下载整理（源目录 → 媒体库标准结构）
- ``aggregate_sync`` — 网盘聚合归拢（副数据源 → 主数据源）
- ``cleanup_link_cache`` — 清理过期直链缓存（L1 内存 + L2 表；302 中转用）
- ``purge_dav_auth_cache`` — 淘汰过期的 WebDAV 认证失败缓存

**数据流触发**（机制已实现，**当前没有作业使用**）：

辅种域的三个作业各自按 cron 独立跑，**没有** ``depends_on`` 边：

- ``fetch_all_sites``       每 10 分钟（拉取 + 站点信息，两段）
- ``push_and_monitor_free`` 10 分钟心跳（推送由作业内部节流 ≥30 分钟）
- ``drain_task_queue``      每 5 分钟

- 边由 :attr:`JobMeta.depends_on` 声明，语义是**触发时机**（不是数据正确性
  前提）：上游失败时下游照旧按自己的 cron 跑。
- 门槛 :attr:`JobMeta.min_trigger_interval_sec` 保证频率不变密。
- **刻意不挂边**：合并「推送 + 监测」后 cron 心跳（10 分钟）已比推送门槛
  （30 分钟）更细，上游触发不会带来更早的推送——挂了就是永不生效的死边
  （理由见 docs/scheduler-dataflow.md §10）。机制与测试保留，供将来
  「真依赖 + 更粗 cron」的作业使用。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.logging import get_logger
from app.services.factory import AdapterFactory
from app.services.push_and_monitor import PUSH_MIN_INTERVAL_SEC
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
#: - ``5,15,…``: push_and_monitor_free（监测的 10 分钟心跳；推送内部节流）
#: - ``*/5``   : drain_task_queue
#: - ``17 */3``: cross_seed_maintenance（辅种维护，合并巡查/磁盘/孤儿）
DEFAULT_CRON: dict[str, str] = {
    "fetch_all_sites": "*/10 * * * *",
    # 免费种推送 + 窗口监测（2026-11-22 合并为一个作业）：
    # **cron 走监测的 10 分钟心跳**，推送靠作业内部节流到 ≥30 分钟
    # （`push_and_monitor.PUSH_MIN_INTERVAL_SEC`）。
    #
    # 为什么心跳必须是 10 分钟：监测要在窗口到期前执行暂停（pre_pause
    # 默认 10 分钟），它不能等——而推送本身有站点限频，10 分钟一轮
    # 只会把限频摊薄，故用内部节流，不用两条 cron 硬凑。
    "push_and_monitor_free": "5,15,25,35,45,55 * * * *",
    "drain_task_queue": "*/5 * * * *",
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
    # 过期数据清理（此前 cleanup_expired / purge_expired 只有测试调用，
    # 生产从不执行 → L2 缓存表与 DAV 失败记录单调增长）：
    # 47 3  每天 03:47（避开 scrape_metadata 的 41 3）
    # 7 *   每小时第 7 分钟（DAV TTL 默认 60s，小时级淘汰足够防 dict 无界）
    "cleanup_link_cache": "47 3 * * *",
    "purge_dav_auth_cache": "7 * * * *",
}


def _current_max_submissions() -> str:
    """聚合归拢的**单轮提交上限**（运行时参数，0 = 不限）。

    注册日志要打**生效值**——写死默认值会在用户改过参数后与实际行为矛盾
    （同 ``_current_pre_pause_minutes`` 的坑）。
    """
    from app.services.runtime_params import lookup_spec

    try:
        from app.services.runtime_params import get_runtime_params

        value = get_runtime_params().get_int("aggregate_max_submissions")
    except Exception:
        spec = lookup_spec("aggregate_max_submissions")
        value = spec.default if spec is not None else 20
    return "不限" if value <= 0 else str(value)


def _current_max_active_downloads() -> str:
    """当前生效的在途下载水位上限（0 = 不限）。

    读的是**内存里的运行时参数**（DB 值在 lifespan 加载后驻留内存），所以
    读失败不是 DB 锁，而是「运行时参数未初始化」或「参数未注册」。

    降级**必须留日志**：静默回落到默认值 10 会让线上水位配置看似生效、
    实际没生效，且无从排查。
    """
    from app.services.runtime_params import get_runtime_params, lookup_spec

    try:
        value = get_runtime_params().get_int("push_max_active_downloads")
    except (RuntimeError, KeyError) as exc:
        logger.warning(
            "读取运行时参数 push_max_active_downloads 失败，回退默认值: %s", exc
        )
        spec = lookup_spec("push_max_active_downloads")
        value = spec.default if spec is not None else 10
    return "不限" if value <= 0 else str(value)


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
    #: 所属业务模块（:data:`app.services.scheduler.JOB_MODULES` 的键）。
    #:
    #: **必填、无默认值**：新增作业必须显式声明它服务哪个业务域——这正是
    #: 「定时任务」页分组展示的判据。留空会让作业落进「其它」块，虽然页面
    #: 能看，但分组意义就丢了，所以用必填字段把它挡在编译期。
    module: str
    #: 作业执行入口工厂：接收 :class:`AdapterFactory`（作业自己按需建
    #: adapter），返回一个**无参可调用**（调度器按零参调用它）。
    make_func: Callable[[AdapterFactory], Callable[[], Awaitable[object]]]
    #: 数据流上游（作业名）：上游成功后按门槛触发本作业（详见
    #: :attr:`app.services.scheduler.JobSpec.depends_on`）。
    #:
    #: **只给真依赖用**：上游产出正是本作业的输入时才挂。别把「时间上常常
    #: 相邻」误当成依赖——那会把两个作业的频率耦死（`cross_seed_maintenance`
    #: 合并的教训）。
    depends_on: str | None = None
    #: 依赖触发的最小间隔（秒）：0 = 上游每次成功都触发；>0 = 门槛。
    min_trigger_interval_sec: int = 0
    #: 追加到注册日志末尾的补充说明（如参数回显）
    log_extra: str = ""
    #: 动态补充说明（**注册时求值**）——参数类回显要用它：
    #: 运行时参数在 lifespan 里先于 register_jobs 初始化，注册时能读到
    #: **DB 里的生效值**（写死在 log_extra 里的话，用户改过参数后日志会
    #: 与实际行为矛盾）。
    log_extra_fn: Callable[[], str] | None = None


#: 全部作业的元数据（**新增作业只需在这里加一行**）。
#:
#: cron 在 :data:`DEFAULT_CRON`（独立常量，便于测试断言覆盖）。
JOB_META: tuple[JobMeta, ...] = (
    JobMeta(
        name="fetch_all_sites",
        title="站点拉取与信息",
        description=(
            "遍历所有启用站点拉取种子列表（写入 torrent_index）"
            "＋ 同步刷新各站用户数据（上传/下载/做种/时魔 → site_user_data）"
        ),
        make_func=lambda f: _job_fn(lambda: _run_fetch_all_sites(f)),
        module="seed",
    ),
    JobMeta(
        name="push_and_monitor_free",
        title="免费种推送与监测",
        description=(
            "巡检已推免费种（临期/非免费/异常/停滞超时自动暂停或删除）＋ "
            "按下载器在途水位动态补推新免费种（距上次推送 ≥30 分钟才推）"
        ),
        make_func=lambda f: _job_fn(lambda: _run_push_and_monitor(f)),
        module="seed",
        # 合并后**不挂 depends_on**：cron 心跳已是 10 分钟，比推送门槛
        # （30 分钟）还细，上游触发不会带来更早的推送——挂了就是永不生效
        # 的边（详见 docs/scheduler-dataflow.md §5）。
        log_extra_fn=lambda: (
            f" (pre_pause={_current_pre_pause_minutes()}min"
            f"，在途上限={_current_max_active_downloads()}"
            f"，推送间隔={PUSH_MIN_INTERVAL_SEC // 60}min)"
        ),
    ),
    JobMeta(
        name="drain_task_queue",
        title="任务队列排空",
        description="回收崩溃残留的僵死任务 + 重试退避到期的推送任务（每 5 分钟）",
        make_func=lambda f: _job_fn(lambda: _run_drain_task_queue(f)),
        module="seed",
    ),
    JobMeta(
        name="cross_seed_maintenance",
        title="辅种维护",
        description=(
            "辅种站巡检（落快照 + 暂停异常种子）→ 磁盘超阈删种子 → 孤儿文件扫描"
            "（每 3 小时）"
        ),
        make_func=lambda f: _job_fn(_run_cross_seed_maintenance),
        module="seed",
        log_extra="（巡查 → 磁盘流转 → 孤儿扫描，三段顺序执行）",
    ),
    JobMeta(
        name="scrape_metadata",
        title="媒体刮削",
        description=(
            "扫描本地媒体库目录，为未刮削的视频补齐 NFO 与图片（TMDB）"
        ),
        make_func=lambda f: _job_fn(_run_scrape_metadata),
        module="media",
    ),
    JobMeta(
        name="organize_files",
        title="下载整理",
        description=(
            "扫描整理源目录里的视频，识别后按标准结构 copy/move 到本地"
            "媒体库，并触发刮削"
        ),
        make_func=lambda f: _job_fn(_run_organize),
        module="media",
    ),
    JobMeta(
        name="aggregate_sync",
        title="聚合归拢",
        description=(
            "把副数据源缓冲的文件搬回主数据源（云下载），"
            "主盘确认落盘后自动清理副盘副本"
        ),
        make_func=lambda f: _job_fn(_run_aggregate_sync),
        module="media",
        log_extra_fn=lambda: f" (单轮上限={_current_max_submissions()})",
    ),
    JobMeta(
        name="cleanup_link_cache",
        title="清理直链缓存",
        description="删除过期的直链缓存（L2 表 + L1 内存），防止缓存表单调增长（每天 03:47）",
        make_func=lambda f: _job_fn(_run_cleanup_link_cache),
        module="media",
    ),
    JobMeta(
        name="purge_dav_auth_cache",
        title="清理 DAV 认证缓存",
        description="淘汰过期的 WebDAV 认证失败记录，防止进程内字典无界增长（每小时）",
        make_func=lambda f: _job_fn(_run_purge_dav_auth_cache),
        module="media",
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
    # 依赖图统一在**全部注册完之后**校验：逐条校验会因注册顺序误报
    # 「上游未注册」（example: push 声明依赖 fetch，而 fetch 此刻还没进表）。
    # 坏图（拼错 / 成环）在启动时就炸，不带着它跑。
    scheduler_center.validate_graph()


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
            # 业务模块随作业下发：「定时任务」页按它分组展示
            module=meta.module,
            # 数据流依赖（上游成功触发；门槛防变密）
            depends_on=meta.depends_on,
            min_trigger_interval_sec=meta.min_trigger_interval_sec,
        )
    )
    effective = scheduler_center.list_job_cron(meta.name)
    extra = meta.log_extra_fn() if meta.log_extra_fn is not None else meta.log_extra
    logger.info("已注册作业：%s cron=%r%s", meta.title, effective, extra)


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
        "app.services.jobs",            # _run_fetch_all_sites（拉取阶段）
        "app.services.user_data",       # _run_fetch_all_sites（信息获取阶段）
        "app.services.job_compose",     # _run_fetch_all_sites / push_and_monitor
        "app.services.push_and_monitor",  # _run_push_and_monitor（合并作业）
        "app.services.push_free",       # 合并作业内部调 PushFreeService
        "app.services.monitor",         # 合并作业内部调 MonitorFreeWindowService
        "app.services.task_drain",      # _run_drain_task_queue
        "app.services.cross_seed_maintenance",  # _run_cross_seed_maintenance
        "app.services.scrape_jobs",     # _run_scrape_metadata（整理刮削迁移）
        "app.services.organize_service",  # _run_organize（整理刮削迁移）
        "app.services.aggregate_sync",  # _run_aggregate_sync（网盘聚合归拢）
        "app.services.svc_factory",     # _run_cleanup_link_cache（get_link_cache）
        "app.services.link_cache",      # get_link_cache() 返回的缓存实现
        "app.services.dav_auth",        # _run_purge_dav_auth_cache
    ):
        importlib.import_module(mod)


# ============================================================ 作业入口
async def _run_fetch_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """站点拉取 + 站点信息获取（2026-11-22 合并为一个作业）。

    两段都遍历同一批**启用站点**，所以合并后共用一条 cron 心跳
    （``*/10``），不再单独为「用户数据」开一个作业：

    1. **站点拉取** → 种子列表写 ``torrent_index``（推送的数据源）
    2. **站点信息获取** → 上传/下载/做种/时魔写 ``site_user_data``
       （推送选种排序的输入）

    顺序：先拉取（推送依赖它），再取信息——两段互不依赖，顺序只是为了
    让「拉取失败」更容易在日志里对齐。

    两段**各自容错**（:func:`app.services.job_compose.run_stage`）：
    一段抛错不影响另一段，失败进 ``steps``。返回的顶层字段保持与合并前
    一致（``total_written`` / ``scanned_sites`` 等），额外的用户数据结果
    放在 ``user_data`` 键里——旧调用方不受影响。
    """
    from app.services.job_compose import build_summary, run_stage
    from app.services.jobs import fetch_all_sites
    from app.services.user_data import refresh_all_sites

    fetch_result, fetch_step = await run_stage(
        "站点拉取",
        lambda: fetch_all_sites(factory),
    )
    user_result, user_step = await run_stage(
        "站点信息获取",
        lambda: refresh_all_sites(factory),
    )

    # 拉取段抛异常时 run_stage 返回空 dict，顶层字段会整批消失；
    # 先垫默认值再被实际结果覆盖，保证旧调用方 / 前端始终读得到。
    merged: dict[str, object] = {
        "total_written": 0,
        "scanned_sites": 0,
        **fetch_result,
    }
    merged.update(
        {
            "summary": build_summary(
                {"fetch": fetch_result, "user_data": user_result},
                ("fetch", "user_data"),
            ),
            # 两段各自带逐站明细 → 挂成两步的 children（前端可展开）
            "steps": [fetch_step, user_step],
            "user_data": user_result,
        }
    )
    return merged


async def _run_push_and_monitor(factory: AdapterFactory) -> dict[str, object]:
    """执行「免费种推送 + 窗口监测」合并作业（2026-11-22）。

    编排与节流在 :mod:`app.services.push_and_monitor` 里：监测每轮都跑，
    推送按 30 分钟门槛节流，两段各自容错。
    """
    from app.services.push_and_monitor import run_push_and_monitor

    return await run_push_and_monitor(factory)


async def _run_drain_task_queue(factory: AdapterFactory) -> dict[str, object]:
    """排空任务队列（崩溃恢复 + 退避重试）。"""
    from app.services.task_drain import drain_task_queue

    return await drain_task_queue(factory)


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


async def _run_cleanup_link_cache() -> dict[str, object]:
    """清理过期直链缓存（L2 表 + L1 内存）。"""
    from app.services.svc_factory import get_link_cache

    deleted = await get_link_cache().cleanup_expired()
    return {"deleted": deleted}


async def _run_purge_dav_auth_cache() -> dict[str, object]:
    """淘汰过期的 WebDAV 认证失败缓存。

    ``purge_expired()`` 是同步的纯内存操作（当前在主 event loop 调用），
    这里直接调用即可，不需要 ``to_thread``。
    """
    from app.services.dav_auth import dav_authenticator

    purged = dav_authenticator.purge_expired()
    return {"purged": purged}


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
