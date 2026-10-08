"""FastAPI 应用骨架（M1）。

生命周期（lifespan）负责：
1. 配置校验（``ENCRYPTION_KEY`` fail-fast，FR-7.1）
2. 数据库初始化（WAL + 单写者，§8）
3. 调度中心启动（M2 起挂载业务任务）
4. 优雅停机：停止新调度 → 等待在途命令（最长 60s，FR-8.2）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response

from app import __version__
from app.core.config import AppSettings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import render_metrics, state
from app.db.session import dispose_db, init_db, writer
from app.services.scheduler import scheduler_center

logger = get_logger(__name__)

#: 停机时等待在途命令的上限
DRAIN_TIMEOUT = 60.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: AppSettings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    logger.info("启动 %s v%s env=%s", settings.app_name, __version__, settings.env)

    grace = float(settings.shutdown_grace_seconds)
    if settings.scheduler_enabled:
        # 停机第一步：停止新任务调度（M2 起有在途作业时才有实际等待）
        scheduler_center.start()
    scheduler_center.accepting_new_work = False

    init_db(settings)
    await writer.create_all()
    state["db_healthy"].set(1 if await writer.healthcheck() else 0)

    try:
        yield
    finally:
        pending = scheduler_center.inflight_count()
        logger.info(
            "收到停机信号，等待在途作业 %d 个（上限 %.0fs）", pending, grace
        )
        if pending:
            await scheduler_center.drain(timeout=grace)
        await dispose_db()
        logger.info("已停机")


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """应用工厂。测试可注入独立 settings。"""
    if settings is not None:
        get_settings.cache_clear()

    app = FastAPI(
        title="SeedHelper",
        version=__version__,
        description="PT 辅种与自动监测系统",
        lifespan=lifespan,
    )

    _mount_routes(app)
    return app


def _mount_routes(app: FastAPI) -> None:
    from app.api.routes import alerts, dashboard, sites, torrents

    for router in (
        dashboard.router,
        sites.router,
        torrents.router,
        alerts.router,
    ):
        app.include_router(router, prefix="/api/v1")

    @app.get("/healthz", tags=["ops"], summary="健康检查（FR-8.2）")
    async def healthz() -> dict[str, object]:
        """容器健康检查端点。数据库异常时返回 unhealthy 以触发重启。"""
        healthy = await writer.healthcheck()
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