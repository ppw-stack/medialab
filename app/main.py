"""FastAPI 应用骨架（M1）。

生命周期（lifespan）负责：
1. 数据库初始化（WAL + 单写者，§8）
2. 调度中心启动（M2 起挂载业务任务）
3. 优雅停机：停止新调度 → 等待在途命令（最长 60s，FR-8.2）
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from app import __version__
from app.core.auth import hash_password, init_hmac_key, is_hashed
from app.core.config import AppSettings, get_settings, set_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import render_metrics, state
from app.db.bootstrap import db_bootstrap
from app.db.session import dispose_db, init_db
from app.services.dav_audit import dav_auditor
from app.services.factory import AdapterFactory, init_factory
from app.services.job_registry import preflight_job_imports, register_jobs
from app.services.scheduler import scheduler_center
from app.webdav.mount import mount_dav

logger = get_logger(__name__)


def _probe_targets() -> list[str]:
    """要探活的外部路径（挂在 CIFS 上的目录）。

    守卫的挂载探活线程会定期列这些目录——挂载卡死时线程卡住、缓存里的
    时间戳变陈旧，``/healthz`` 的 ``mounts`` 字段据此显示 ``hang``，
    面板**不会**跟着假死。
    """
    from app.services.env_settings import get_env_setting
    from app.services.startup_status import record

    targets: list[str] = []
    for key in ("cross_seed_scan_dirs",):
        try:
            raw = get_env_setting(key)
        except Exception as exc:
            # 此前是静默 continue——挂载探活目录读不到，/readyz 却照样 ok。
            record(f"probe_targets:{key}", exc)
            continue
        for part in raw.split(","):
            part = part.strip()
            if part:
                targets.append(part)
    return targets


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: AppSettings = get_settings()
    # 启动降级台账：每次启动从零开始，避免测试里多次 create_app 串状态。
    from app.services.startup_status import reset

    reset()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        log_dir=settings.log_dir,
    )
    logger.info("启动 %s v%s env=%s", settings.app_name, __version__, settings.env)

    grace = float(settings.shutdown_grace_seconds)

    # 适配器工厂必须先于引擎就绪（FR-7）
    factory: AdapterFactory = init_factory()

    # 从 WEB_TOKEN 派生 session HMAC 密钥（防硬编码盐）
    if not settings.web_token:
        raise RuntimeError("WEB_TOKEN 未配置，无法派生 session HMAC 密钥")
    init_hmac_key(settings.web_token.get_secret_value())

    # /302 中转 URL 的 HMAC 签名密钥——与 WEB_TOKEN 同等级必填。
    # 旧实现不在启动时校验：空密钥能正常监听，直到生成 STRM / 播放才 500；
    # 更糟的是库健康检查会用空串重算签名，把全库误判为「签名无效」，
    # 诱导运维执行批量重签（改写所有 STRM）。
    if not settings.signing_secret or not settings.signing_secret.get_secret_value().strip():
        raise RuntimeError("SIGNING_SECRET 未配置，无法签发 /302 中转 URL")

    # 首次启动：明文密码升级为 PBKDF2 哈希（与 web_password 字段存储兼容）
    from pydantic import SecretStr

    if settings.web_password and not is_hashed(settings.web_password.get_secret_value()):
        try:
            plain = settings.web_password.get_secret_value()
            settings.web_password = SecretStr(hash_password(plain))
            logger.info("已将默认 WEB_PASSWORD 升级为 PBKDF2 哈希")
        except Exception as exc:
            logger.warning("密码升级失败：%s", exc)
            from app.services.startup_status import record

            record("web_password_upgrade", exc)

    init_db(settings)
    # 建表 + 全部迁移（幂等）。含 FR-1.5 adapter 值迁移 + 媒体库域的迁移。
    await db_bootstrap.create_all()

    # 单实例租约：本项目依赖大量进程内状态（调度中心、作业表、watch_manager
    # 的全局字典、QR session），多 worker / 多副本会让它们各自为政 →
    # 作业重复执行、任务 404。Dockerfile 的 --workers 1 只是部署约定，
    # 这里在 DB 层强制只有一个实例能继续启动（测试环境跳过）。
    from app.services.instance_lease import acquire_instance_lease

    lease = await acquire_instance_lease()
    app.state.instance_lease = lease

    state["db_healthy"].set(1 if await db_bootstrap.healthcheck() else 0)

    # 运行时参数（DB 持久化的可调设置）必须在 register_jobs 之前初始化，
    # 让作业函数首次运行就能拿到最新值。
    #
    # 注意：**不放在 scheduler_enabled 分支里**——运行时参数是 API 层
    # 也会读的配置（GET/PUT /api/v1/scheduler/runtime-params），调度器
    # 关闭时该端点仍要可用。之前放在 if 内，scheduler_enabled=False
    # 时端点直接 500（RuntimeError: 运行时参数未初始化）。
    from app.services.runtime_params import (
        init_runtime_params_async,
        set_runtime_params,
    )

    set_runtime_params(await init_runtime_params_async())

    # 环境变量配置（DB 持久化的字符串型设置，如辅种扫描目录）同样
    # 必须在调度作业跑之前初始化——孤儿扫描作业会读它。
    #
    # 同样**不放在 scheduler_enabled 分支里**——API 层的 GET/PUT
    # /env-settings 端点在调度器关闭时仍要可用。
    from app.services.env_settings import (
        init_env_settings_async,
        set_env_settings,
    )

    set_env_settings(await init_env_settings_async())

    # 作业模块预热（**必须在调度启动之前**）：各作业入口是函数内惰性导入，
    # 第一次触发会在事件循环线程上同步读盘加载整条模块链——QNAP 冷存储上
    # 一次几分钟，循环冻结、面板假死（2026-11-07 prod 事故，172）。
    # 在线程里提前预热：预热线程读盘期间循环保持响应，此后作业触发的导入
    # 全部命中 sys.modules。调度关闭时也执行——手动触发作业走同一批模块。
    await asyncio.to_thread(preflight_job_imports)

    if settings.scheduler_enabled:
        # 作业调度配置（DB 持久化的启停 + cron）同样必须在 register_jobs
        # 之前载入：register() 会按 enabled 决定是否把作业安排到
        # APScheduler、按 cron 决定触发时刻，届时才知道该用什么。
        from app.services.job_registry import JOB_META
        from app.services.job_settings import (
            load_crons,
            load_enabled_states,
            merge_job_settings,
            migrate_renamed_job,
            prune_orphaned_settings,
        )

        # 作业合并/改名后迁移旧的配置行（用户改过的启停 + cron 不丢），
        # 再清理已下线作业的残留行。**必须在 apply_overrides 之前**——
        # 否则旧行读出来没人用，新作业又拿不到用户改过的值。
        known_jobs = {meta.name for meta in JOB_META}
        renamed = await migrate_renamed_job(
            "scan_cross_seed", "cross_seed_maintenance", known_jobs
        )
        # 2026-11-22：免费种推送 + 窗口监测合并为一个作业（两旧 → 一新，
        # 启停取 AND：任一边被停用过，新作业也停用——不静默复活用户关掉的
        # 行为）。
        merged = await merge_job_settings(
            ["push_free_torrents", "monitor_free_window"],
            "push_and_monitor_free",
            known_jobs,
        )
        # 迁移**没成功** → 旧配置行留着（下次启动重试），别让 prune 当孤儿
        # 删掉——删了用户的启停状态就永久没了（新作业会按默认启用复活）。
        keep: set[str] = set()
        if not renamed:
            keep.add("scan_cross_seed")
        if not merged:
            keep |= {"push_free_torrents", "monitor_free_window"}
        await prune_orphaned_settings(known_jobs, keep=keep or None)

        scheduler_center.apply_overrides(
            await load_enabled_states(),
            await load_crons(),
        )
        scheduler_center.start()
        register_jobs(factory)

    # ============================================================ 媒体库域
    #
    # 中转服务（直链缓存 + /302）——媒体库域的服务单例。
    from app.services.svc_factory import init_services

    init_services(settings)

    # 媒体库域的请求限速（按驱动分桶）。
    from app.core.rate_limit import configure_provider_rate_limit

    configure_provider_rate_limit("guangya", settings.guangya_rate_limit_interval_sec)
    # 换直链单独一个桶：官方限频 2 次/秒，比列目录的 5 次/秒严
    configure_provider_rate_limit(
        "guangya_link", settings.guangya_link_rate_limit_interval_sec
    )

    # WebDAV 审计后台协程（批量落库）
    if settings.dav_enabled:
        dav_auditor.start()

    # 上传任务监听器（进程内常驻协程）
    #
    # DB 是真相源：启动时按 ``upload_tasks`` 表拉起所有启用的任务。
    # 监听失败不阻塞启动——面板上能看到错误，改配置后重启即可。
    from app.services import watch_manager

    await watch_manager.start_all()

    # 存储源启动测连（后台并发，不阻塞启动）
    #
    # 所有启用且有凭据的光鸭驱动自动测连一次，结果缓存供
    # ``GET /drivers`` 携带——面板打开即见连通状态，不用逐个手动点。
    # fire-and-forget：失败只记日志；任务引用挂到 app.state 防 GC。
    from app.services.driver_service import probe_all_enabled

    app.state.driver_probe_task = asyncio.create_task(probe_all_enabled())

    # 运行时守卫（心跳看门狗 + 挂载探活）
    #
    # 2026-11-06 prod 事故：CIFS 挂载卡死 → 事件循环里的一次同步 stat
    # 把 loop 卡住 → 面板假死、healthz 被 RST、容器 unhealthy。守卫做两件事：
    # ① loop 心跳滞后超阈值 → 自动 py-spy dump 留证；② 挂载探活 → 挂载卡死
    # 在 ``/healthz`` 可见（而不是整个面板不可用）。
    from app.services import runtime_guard

    runtime_guard.start(asyncio.get_running_loop(), mounts=_probe_targets())

    # 启动降级告警（仅在存在降级且配置了 ALERT_WEBHOOK_URL 时异步发一次）
    from app.services import startup_status

    startup_degradations = startup_status.snapshot()
    if startup_degradations:
        from app.services.alerting import notify_startup_degradations

        app.state.degradation_alert_task = asyncio.create_task(
            notify_startup_degradations(startup_degradations)
        )

    try:
        yield
    finally:
        logger.info("停机中…")
        # 后台 fire-and-forget 任务必须先取消再收尾。
        #
        # 与下面的 ``driver_probe_task`` 同构：进程停机时被丢弃的 task 会
        # 留下「Task was destroyed but it is pending」/ 异常无人回收；若它
        # 落到 DB，还会在 ``dispose_db()`` 之后报 RuntimeError 被吞掉。
        alert_task: asyncio.Task[object] | None = getattr(
            app.state, "degradation_alert_task", None
        )
        if alert_task is not None and not alert_task.done():
            alert_task.cancel()
            await asyncio.gather(alert_task, return_exceptions=True)
        # 启动测连任务要先取消——它会往 DB 写探测历史。
        #
        # **必须在 dispose_db() 之前**：``dispose_db()`` 把 session factory
        # 置 None 后，任务若正好跑到落库那一步 → ``RuntimeError("数据库未
        # 初始化")``。而 ``probe_driver`` 的落库 try/except 只记 warning
        # → 异常被吞掉，不影响退出 → 表现为「探测历史偶尔少一条」的
        # 静默故障（最难查的那种）。
        probe_task: asyncio.Task[object] | None = getattr(
            app.state, "driver_probe_task", None
        )
        if probe_task is not None and not probe_task.done():
            probe_task.cancel()
            # return_exceptions=True → 子任务的 CancelledError 当作结果
            # 返回，不会从 await 抛出来（也不会吞掉外层的取消信号）。
            await asyncio.gather(probe_task, return_exceptions=True)
        scheduler_center.accepting_new_work = False
        pending = scheduler_center.inflight_count()
        logger.info("收到停机信号，等待在途作业 %d 个（上限 %.0fs）", pending, grace)
        if pending:
            await scheduler_center.drain(timeout=grace)
        await scheduler_center.shutdown(grace_seconds=int(grace))
        await factory.aclose_all()
        # 运行时守卫收尾
        from app.services import runtime_guard

        runtime_guard.stop()
        # WebDAV 审计队列里剩余的条目落库
        if settings.dav_enabled:
            await dav_auditor.close()
        await watch_manager.stop_all()
        # 租约必须在 dispose_db() 之前释放——释放要写 DB；且要先停心跳。
        instance_lease = getattr(app.state, "instance_lease", None)
        if instance_lease is not None:
            await instance_lease.close()
        await dispose_db()
        logger.info("已停机")


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """应用工厂。测试可注入独立 settings。"""
    if settings is not None:
        # 注入全局 settings（override 通道）。
        # 原先这里是 get_settings.cache_clear()——但 get_settings 是
        # **无参**的 lru_cache 工厂，cache_clear() 只让它下次重读 .env，
        # 传入的 settings 对象被完全丢弃，注入形同虚设（测试无法用独立
        # DB，会写到 /app/data）。现改用 set_settings() 建立真正的覆盖。
        set_settings(settings)

    app = FastAPI(
        title="SeedHelper",
        version=__version__,
        description="PT 辅种与自动监测系统",
        lifespan=lifespan,
    )

    # CORS：同源部署（SPA 由 FastAPI 托管）默认不启用；
    # 分离部署时用 WEB_CORS_ORIGINS 逗号分隔显式声明。
    # 旧默认（web_host:web_port）会把 0.0.0.0 拼进 allow_origins，浏览器
    # 永远匹配不上，等于 CORS 全挂——废弃。
    origins = [
        o.strip()
        for o in get_settings().web_cors_origins.split(",")
        if o.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    from app.api.errors import install_exception_handlers

    _mount_routes(app)
    install_exception_handlers(app)

    # WebDAV —— **必须在 SPA 兜底之前挂载**。
    #
    # 路由按注册顺序匹配，而 SPA 的 ``/{full_path:path}`` 会吞掉一切路径。
    # 挂晚了 DAV 就被 SPA 吞掉（返回 index.html），客户端报「网络异常」。
    mount_dav(app, get_settings())

    _mount_frontend(app, get_settings().frontend_dist)
    return app


def _mount_routes(app: FastAPI) -> None:
    from app.api.auth import require_session
    from app.api.auth import router as auth_router
    from app.api.routes import (
        aggregate,
        cross_seed,
        dav_users,
        downloaders,
        drivers,
        env_settings,
        health,
        media,
        migrations,
        redirect,
        scan_jobs,
        scheduler,
        scrape,
        sites,
        system,
        torrent_index,
        upload_tasks,
        user_data,
    )

    # -- 辅种域（/api/v1/*）--
    for router in (
        sites.router,
        downloaders.router,
        scheduler.router,
        cross_seed.router,
        torrent_index.router,
        user_data.router,
        env_settings.router,
    ):
        app.include_router(router, prefix="/api/v1")
    app.include_router(auth_router)  # 自身已含 /api/v1 前缀

    # -- 媒体库域（/api/*）--
    #
    # 这批路由**自带 prefix**（如 ``/api/media``），故不再传 prefix。
    # 与辅种域的 ``/api/v1/*`` **零冲突**。
    for router in (
        system.router,
        drivers.router,
        aggregate.router,
        media.router,
        scrape.router,
        health.router,
        scan_jobs.router,
        migrations.router,
        upload_tasks.router,
        dav_users.router,
        # 中转路由挂根路径：/302/<media_id>/<sig>
        redirect.router,
    ):
        app.include_router(router)

    # 指标里含站点 / 下载器 / 作业状态等运行画像——与其余管理端点一致，
    # 必须登录（ Prometheus 抓取端请配 Bearer WEB_TOKEN 或放在反代内网）。
    @app.get(
        "/metrics",
        tags=["ops"],
        summary="Prometheus 指标（FR-8.3）",
        dependencies=[Depends(require_session)],
    )
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)


def _mount_frontend(app: FastAPI, dist_dir: Path) -> None:
    """挂载前端静态资源（SPA）。

    **必须处理 history 模式的深链接**——``/library``、``/settings`` 这些
    前端路由在浏览器刷新时是**服务端路径**，若直接 404，用户一刷新就白屏。

    用自定义 handler 而不是 ``StaticFiles(html=True)``：后者只对**目录**
    回退到 index.html，路径下的**文件**（如 ``/library``）仍会 404。
    """
    path = Path(dist_dir)
    if not path.is_dir():
        logger.info("前端资源目录不存在，跳过挂载：%s", path)
        return

    index_file = path / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        """静态资源优先；找不到就回 index.html（交给前端路由）。

        **静态资源与 index.html 要分开处理**：

        - **带 hash 的资源**（``/assets/index-xxxx.js``）→ **长缓存 + 不可变**
          （内容变了 hash 就会变，URL 变了就不该再缓存旧的）
        - **不存在的资源** → **真 404**，**不能**回 index.html！

          回 index.html 会让浏览器把 HTML 当 JS 解析 → 整页白屏。
          这正是「更新服务后浏览器访问失败」的原因：浏览器缓存了旧
          index.html → 引用旧 hash 的 JS → 该文件已删 → fallback 返回
          HTML → JS 解析失败。
        """
        # API 与运维路径**不接管**——它们由 FastAPI 自身处理。
        # 未匹配到的 API 路径返回 JSON 404，而不是 index.html。
        #
        # ⚠️ **``assets`` 不在此列**——它是前端构建产物的真实目录
        # （``/assets/index-xxxx.js``），必须正常返回（见下方 is_file 分支）。
        if full_path.startswith(
            ("api/", "healthz", "readyz", "metrics", "docs", "openapi")
        ):
            return JSONResponse({"detail": "not found"}, status_code=404)

        if full_path:
            # 防目录穿越：解析后必须仍在 dist 目录内
            candidate = (path / full_path).resolve()
            try:
                candidate.relative_to(path.resolve())
            except ValueError:
                return Response(status_code=404)
            if candidate.is_file():
                # 带 hash 的构建产物 → 不可变缓存
                if full_path.startswith("assets/"):
                    return FileResponse(
                        candidate,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"},
                    )
                return FileResponse(candidate)

            # **资源不存在** → 真 404（不能回 index.html —— 见 docstring）
            if full_path.startswith("assets/"):
                return Response(status_code=404, content="资源不存在")

        if not index_file.is_file():
            return Response(status_code=404, content="前端资源缺失")
        # index.html 本身**不缓存** —— 保证下次刷新能拿到新的资源引用
        return FileResponse(index_file, headers={"Cache-Control": "no-store"})

    logger.info("已挂载前端资源：%s", path)


app = create_app()