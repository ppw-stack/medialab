"""调度作业配置的持久化（DB）。

:class:`~app.services.scheduler.JobSpec` 的 ``enabled`` 与 ``cron`` 只存在
于内存——容器重启后作业会被
:func:`~app.services.job_registry.register_jobs` 重新注册为默认值，用户在
UI 里的修改会"复活"。

本模块把调度配置落库，启动期回填，让用户的选择跨重启保持。

设计取舍：

- **独立表**而非复用 :class:`app.db.models.AppRuntimeParam`：后者是
  int-only 的 kv 表（``value_int`` / ``value_str``），塞 bool / cron
  不合语义，且它承载的是"作业运行参数"（如剩余小时数），不是"调度配置"。
- **只存用户显式改过的作业**：DB 里没有的作业用注册时的默认值
  （``enabled=True`` + 代码里的 cron），新增作业不会被历史行影响；
  已下线作业的残留行读出时忽略。
- **DB 写失败不回滚内存**：配置已在调度中心生效，回滚需要反向操作
  APScheduler 的 job 槽，实现复杂且可能失败。改为让 API 层返 500，
  用户能立刻知道"生效了但没存住"。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import SchedulerJobSetting
from app.db.session import session_scope

logger = get_logger(__name__)

__all__ = [
    "JobSettings",
    "load_crons",
    "load_enabled_states",
    "load_job_settings",
    "save_cron",
    "save_enabled_state",
]


@dataclass(frozen=True, slots=True)
class JobSettings:
    """单个作业的调度配置（DB 行）。"""

    job_name: str
    enabled: bool = True
    #: cron 表达式；``None`` = 用代码里的默认值
    cron: str | None = None


async def load_job_settings() -> dict[str, JobSettings]:
    """读所有作业的调度配置（enabled + cron）。

    Returns: ``{作业名: JobSettings}``——只含 DB 里存在的行。

    读取失败不抛——启动期不应因这一项失败而起不来，降级为"全部默认"
    并告警。
    """
    try:
        async with session_scope() as session:
            rows = (
                await session.execute(select(SchedulerJobSetting))
            ).scalars().all()
            return {
                row.job_name: JobSettings(
                    job_name=row.job_name,
                    enabled=bool(row.enabled),
                    cron=row.cron,
                )
                for row in rows
            }
    except Exception as exc:
        logger.warning(
            "读取作业调度配置失败，本次启动全部作业按默认处理：%s", exc
        )
        return {}


async def load_enabled_states() -> dict[str, bool]:
    """从 DB 读各作业的启停状态（兼容入口）。

    Returns: ``{作业名: enabled}``——只含 DB 里存在的行。DB 为空时返回
    空 dict（全部作业用默认的 ``enabled=True``）。
    """
    return {name: s.enabled for name, s in (await load_job_settings()).items()}


async def load_crons() -> dict[str, str]:
    """从 DB 读各作业的 cron 覆盖值。

    Returns: ``{作业名: cron}``——**只含 cron 非 NULL 的行**。NULL 表示
    "用代码里的默认值"，不该覆盖。
    """
    return {
        name: s.cron
        for name, s in (await load_job_settings()).items()
        if s.cron
    }


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


async def save_cron(job_name: str, cron: str) -> None:
    """把作业的 cron 表达式写入 DB（upsert）。

    **不碰 enabled**：cron 与启停是两件独立的事，各自的保存入口只改
    自己那列（否则「改 cron」会顺带把停用的作业启用）。

    :raises Exception: DB 写失败时向上抛，由调用方决定如何响应。
    """
    async with session_scope() as session:
        row = await session.get(SchedulerJobSetting, job_name)
        if row is None:
            session.add(
                SchedulerJobSetting(job_name=job_name, enabled=True, cron=cron)
            )
        else:
            row.cron = cron
