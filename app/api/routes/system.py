"""系统状态与探针。

- ``/healthz`` —— **存活探针**（容器 healthcheck 用）
- ``/readyz`` —— **就绪探针**（DB 可用 + 服务已初始化）

**为什么 ``/healthz`` 不因 DB 异常而报 unhealthy**（合并说明）：

两个域对 ``/healthz`` 的约定不同——辅种域原本查 DB（DB 挂了就返
``unhealthy``），媒体库域不查（纯存活）。合并后取**媒体库域的口径**：

容器 healthcheck 的语义是「**进程还活着吗**」。若 DB 短暂抖动就报
unhealthy，Docker 会**重启容器**——而 SQLite 的短暂锁等待（写事务
独占整库时）恰恰会触发它。DB 健康与否属于「就绪」，归 ``/readyz``。

``/healthz`` 仍然**报告** DB 状态（``db`` 字段），只是不用它决定
status —— 排障时一眼能看出是不是 DB 的问题。
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db.session import get_engine
from app.services.svc_factory import get_link_cache

router = APIRouter(tags=["system"])


@router.get("/healthz")
async def healthz() -> dict[str, object]:
    """存活探针：进程活着就返回 ok（**不**因 DB 抖动而失败）。

    ``db`` 字段报告 DB 当前是否可用——供排障用，不影响 ``status``。
    """
    db_ok = await _db_ok()
    return {
        "status": "ok",
        "db": db_ok,
    }


@router.get("/readyz")
async def readyz() -> dict[str, object]:
    """就绪探针：DB 可用 + 服务已初始化。

    ``status`` 在 DB 不可用时为 ``degraded``——**不触发容器重启**，
    但可以被人 / 监控系统看到。
    """
    db_ok = await _db_ok()
    payload: dict[str, object] = {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
    }
    try:
        payload["cache"] = get_link_cache().stats()
    except RuntimeError:
        payload["cache"] = None
    return payload


async def _db_ok() -> bool:
    """DB 是否可用（能 SELECT 1 即认为可用）。"""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
