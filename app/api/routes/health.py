"""健康检查路由。

**只读**——不修改任何东西（修复 / 重建 / 重签是独立的动作端点）。

DB 访问在 service 层（:func:`app.services.health.library_health_report`），
路由层不碰 ``app.db``——符合分层约束（``api`` 不依赖 ``db``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_session
from app.services.health import library_health_report

router = APIRouter(
    prefix="/api/health", tags=["health"], dependencies=[Depends(require_session)]
)


@router.get("/library")
async def library_health() -> dict[str, object]:
    """媒体库健康检查（只读，不做任何修改）。"""
    report = await library_health_report()
    return report.to_dict()
