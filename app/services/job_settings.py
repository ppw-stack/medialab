"""调度作业启停状态的持久化（DB）。

:class:`~app.services.scheduler.JobSpec.enabled` 只存在于内存——容器重启后
作业会被重新注册为 ``enabled=True``，用户在 UI 里手动停用的作业会"复活"。
本模块把开关状态落库，启动期回填，让停用状态跨重启保持。

设计取舍：

- **独立表**而非复用 :class:`~app.db.models.AppRuntimeParam`：后者是
  int-only 的 kv 表（``value_int`` / ``value_str``），塞 bool 不合语义，
  且它承载的是"作业运行参数"（如剩余小时数），不是"作业开关"。
- **只存用户显式改过的作业**：DB 里没有的作业用注册时的默认值，
  新增作业不会被历史行影响；已下线作业的残留行读出时忽略。
- **DB 写失败不回滚内存**：开关已在调度中心生效，回滚需要反向操作
  APScheduler 的 job 槽，实现复杂且可能失败。改为让 API 层返 500，
  用户能立刻知道"生效了但没存住"。
"""
from __future__ import annotations

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import SchedulerJobSetting
from app.db.session import session_scope

logger = get_logger(__name__)


async def load_enabled_states() -> dict[str, bool]:
    """从 DB 读各作业的启停状态。

    Returns: ``{作业名: enabled}``——只含 DB 里存在的行。DB 为空时返回
    空 dict（全部作业用默认的 ``enabled=True``）。

    读取失败不抛——启动期不应因这一项失败而起不来，降级为"全部默认启用"
    并告警。
    """
    try:
        async with session_scope() as session:
            rows = (
                await session.execute(select(SchedulerJobSetting))
            ).scalars().all()
            return {row.job_name: bool(row.enabled) for row in rows}
    except Exception as exc:
        logger.warning(
            "读取作业启停状态失败，本次启动全部作业按默认启用处理：%s", exc
        )
        return {}


async def save_enabled_state(job_name: str, enabled: bool) -> None:
    """把作业启停状态写入 DB（upsert）。

    :raises Exception: DB 写失败时向上抛，由调用方决定如何响应。
    """
    async with session_scope() as session:
        row = await session.get(SchedulerJobSetting, job_name)
        if row is None:
            session.add(
                SchedulerJobSetting(job_name=job_name, enabled=enabled)
            )
        else:
            row.enabled = enabled
