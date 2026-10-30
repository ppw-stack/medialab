"""健康检查路由。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_session
from app.core.config import get_settings
from app.db.session import session_scope
from app.services.health import check_library

router = APIRouter(
    prefix="/api/health", tags=["health"], dependencies=[Depends(require_session)]
)


async def _db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as db:
        yield db


@router.get("/library")
async def library_health(db: AsyncSession = Depends(_db)) -> dict[str, object]:
    """媒体库健康检查（只读，不做任何修改）。"""
    report = await check_library(db, get_settings())
    return report.to_dict()
