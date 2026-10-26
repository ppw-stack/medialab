"""FastAPI 应用骨架（M1）。

生命周期（lifespan）负责：
1. 数据库初始化（WAL + 单写者，§8）
2. 调度中心启动（M2 起挂载业务任务）
3. 优雅停机：停止新调度 → 等待在途命令（最长 60s，FR-8.2）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app import __version__
from app.core.auth import hash_password, init_hmac_key
from app.core.config import AppSettings, get_settings, set_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import render_metrics, state
from app.db.bootstrap import db_bootstrap
from app.db.session import dispose_db, init_db
from app.services.factory import AdapterFactory, init_factory
from app.services.job_registry import register_jobs
from app.services.scheduler import scheduler_center

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: AppSettings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    logger.info("启动 %s v%s env=%s", settings.app_name, __version__, settings.env)

    grace = float(settings.shutdown_grace_seconds)

    # 适配器工厂必须先于引擎就绪（FR-7）
    factory: AdapterFactory = init_factory()

    # 从 WEB_TOKEN 派生 session HMAC 密钥（防硬编码盐）
    if not settings.web_token:
        raise RuntimeError("WEB_TOKEN 未配置，无法派生 session HMAC 密钥")
    init_hmac_key(settings.web_token.get_secret_value())

    # 首次启动：明文密码升级为 PBKDF2 哈希（与 web_password 字段存储兼容）
    from pydantic import SecretStr

    if settings.web_password and not settings.web_password.get_secret_value().startswith(
        "pbkdf2$"
    ):
        try:
            plain = settings.web_password.get_secret_value()
            settings.web_password = SecretStr(hash_password(plain))
            logger.info("已将默认 WEB_PASSWORD 升级为 PBKDF2 哈希")
        except Exception as exc:
            logger.warning("密码升级失败：%s", exc)

    init_db(settings)
    # 建表 + 全部迁移（幂等）。含 FR-1.5 adapter 值迁移。
    await db_bootstrap.create_all()
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

    if settings.scheduler_enabled:
        # 作业调度配置（DB 持久化的启停 + cron）同样必须在 register_jobs
        # 之前载入：register() 会按 enabled 决定是否把作业安排到
        # APScheduler、按 cron 决定触发时刻，届时才知道该用什么。
        from app.services.job_settings import load_crons, load_enabled_states

        scheduler_center.apply_overrides(
            await load_enabled_states(),
            await load_crons(),
        )
        scheduler_center.start()
        register_jobs(factory)

    try:
        yield
    finally:
        scheduler_center.accepting_new_work = False
        pending = scheduler_center.inflight_count()
        logger.info("收到停机信号，等待在途作业 %d 个（上限 %.0fs）", pending, grace)
        if pending:
            await scheduler_center.drain(timeout=grace)
        await scheduler_center.shutdown(grace_seconds=int(grace))
        await factory.aclose_all()
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

    from app.api.auth import install_static
    from app.api.errors import install_exception_handlers

    _mount_routes(app)
    install_exception_handlers(app)
    install_static(app, path=str(get_settings().frontend_dist))
    return app


def _mount_routes(app: FastAPI) -> None:
    from app.api.auth import router as auth_router
    from app.api.routes import (
        cross_seed,
        dashboard,
        downloaders,
        env_settings,
        scheduler,
        sites,
        torrent_index,
        user_data,
    )

    for router in (
        dashboard.router,
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

    @app.get("/healthz", tags=["ops"], summary="健康检查（FR-8.2）")
    async def healthz() -> dict[str, object]:
        """容器健康检查端点。数据库异常时返回 unhealthy 以触发重启。"""
        healthy = await db_bootstrap.healthcheck()
        state["db_healthy"].set(1 if healthy else 0)
        return {
            "status": "ok" if healthy else "unhealthy",
            "version": __version__,
            "db": healthy,
        }

    @app.get("/metrics", tags=["ops"], summary="Prometheus 指标（FR-8.3）")
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)


app = create_app()