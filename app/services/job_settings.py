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
- **删除旧行前先留快照**：作业合并 / 改名 / 孤儿清理会 ``DELETE`` 旧行
  （不可逆）。删除前把旧行内容写进 ``scheduler_job_settings_backup``
  （**与 DELETE 同一事务**）并打一条启动日志——见 :func:`_snapshot_settings`。
  恢复步骤见 ``CHANGELOG.md`` 的「回滚 / 恢复」。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import SchedulerJobSetting, SchedulerJobSettingBackup
from app.db.session import session_scope
from app.services import startup_status

logger = get_logger(__name__)

__all__ = [
    "JobSettings",
    "load_crons",
    "load_enabled_states",
    "load_job_settings",
    "merge_job_settings",
    "migrate_renamed_job",
    "prune_orphaned_settings",
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


async def _snapshot_settings(
    session: AsyncSession,
    rows: Sequence[SchedulerJobSetting],
    reason: str,
) -> None:
    """删除前把旧行内容**留快照**（不可逆迁移的恢复依据）。

    三条删除路径（合并 / 孤儿清理 / 改名迁移）都走这里——**每条路径都先
    留快照**，不留快照就不删。

    快照落在两处：

    1. **快照表**（``scheduler_job_settings_backup``）——与 ``DELETE`` 在
       **同一个事务**里：要么都成、要么都回滚 → 不会出现「删了却没记录」。
       比日志可靠（日志会轮转、会随容器重建而丢），且能用 SQL 还原。
    2. **启动日志**——人读的线索（``作业配置删除快照（…）``）。

    快照写失败 → 事务回滚 → 旧行**不删**（本次迁移放弃，下次启动重试），
    失败会登记到 ``startup_status``（由调用方处理）。

    恢复步骤见 ``CHANGELOG.md`` 的「回滚 / 恢复」小节。

    :raises Exception: 快照写入（``session.get`` / ``session.add``）或其它
        session 操作失败时向上抛——**绝不静默吞掉**；调用方据此回滚事务
        （旧行因此不删）并登记 ``startup_status`` 降级。
    """
    for row in rows:
        backup = await session.get(SchedulerJobSettingBackup, row.job_name)
        if backup is None:
            session.add(
                SchedulerJobSettingBackup(
                    job_name=row.job_name,
                    enabled=bool(row.enabled),
                    cron=row.cron,
                    reason=reason,
                )
            )
        else:
            backup.enabled = bool(row.enabled)
            backup.cron = row.cron
            backup.reason = reason
            backup.deleted_at = bj_now()
        logger.info(
            "作业配置删除快照（%s）：job_name=%s enabled=%s cron=%r",
            reason,
            row.job_name,
            bool(row.enabled),
            row.cron,
        )


async def merge_job_settings(
    old_names: list[str],
    new_name: str,
    known_jobs: set[str],
) -> bool:
    """把**多个**旧作业的启停状态合并到新作业（作业合并时用）。

    例：``push_free_torrents`` + ``monitor_free_window`` → ``push_and_monitor_free``
    （2026-11-22 合并）。

    与 :func:`migrate_renamed_job` 的差别只有两点：

    1. 旧名是**一组**；
    2. 启停取 **AND**（任一旧作业被用户停用过 → 新作业也停用）。

    **为什么是 AND 而不是 OR**：合并后的作业同时承担两边的动作，任一边
    被停用都说明用户不想要那半边行为——而合并作业没有「只停半边」的开关。
    取 AND = 「只有两边都开着才自动开」，宁可少跑一轮让用户显式打开，
    也不要在用户不知情时恢复他关掉的行为（推送会占带宽/额度，监测会暂停
    种子，两边都不该被静默复活）。

    **不搬 cron**：合并后频率语义变了（原来是两条不同节奏的 cron），
    沿用任何一条都是错的——用代码里的新默认值（10 分钟心跳 + 内部节流）。

    目标行已存在 → **不覆盖**（用户可能已经在新作业上改过）。

    Returns:
        是否发生了迁移（有旧行被消费）。
    """
    from sqlalchemy import delete

    try:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(SchedulerJobSetting).where(
                        SchedulerJobSetting.job_name.in_(old_names)
                    )
                )
            ).scalars().all()
            if not rows:
                return False
            if new_name not in known_jobs:
                # 新作业还没注册 → 不动（等它真被注册时再说）
                return False

            enabled = all(bool(r.enabled) for r in rows)
            target = await session.get(SchedulerJobSetting, new_name)
            if target is None:
                session.add(
                    SchedulerJobSetting(
                        job_name=new_name,
                        enabled=enabled,
                        cron=None,
                    )
                )
            # 删除前先留快照（不可逆 → 事后能回答「删了什么」）；快照与
            # DELETE 同一事务 → 写不进去就整体回滚，旧行不删
            await _snapshot_settings(session, rows, "合并到新作业")
            await session.execute(
                delete(SchedulerJobSetting).where(
                    SchedulerJobSetting.job_name.in_(old_names)
                )
            )
            logger.info(
                "作业配置已合并：%s → %s（enabled=%s，cron 用新默认值）",
                " + ".join(sorted(old_names)),
                new_name,
                enabled,
            )
            return True
    except Exception as exc:
        logger.warning("合并作业配置失败（可忽略）：%s", exc)
        # 启动降级必须登记（AGENTS.md）：/readyz 的 degradations 要能看见
        startup_status.record("job_settings_merge", exc)
        return False


async def prune_orphaned_settings(
    known_jobs: set[str], *, keep: set[str] | None = None
) -> int:
    """删掉**已下线作业**的残留行，返回删除条数。

    **为什么需要**：作业合并/下线后，DB 里旧作业名的行会变成孤儿——
    读出来没人用（``apply_overrides`` 只认已注册的作业），还占着
    ``job_name`` 主键。用户手动改过的启停状态也会跟着丢（新作业用
    默认值），所以**要迁移而不是直接删**——见 :func:`migrate_renamed_job`。

    **``keep`` 里的作业名不删**：用于「迁移尚未完成」的场景。合并旧作业
    配置失败时，旧行必须留着给下次启动重试——删掉等于把用户的启停状态
    永久丢了（新作业会按默认 ``enabled=True`` 复活）。

    删除前会把旧行内容**留快照**（快照表 + 日志，见
    :func:`_snapshot_settings`）——三条删除路径都先留快照，不留就不删。

    删除失败只记日志——启动期不该因清理失败而中断（但会登记到
    ``startup_status``，让 ``/readyz`` 看得见）。
    """
    from sqlalchemy import delete

    try:
        async with session_scope() as session:
            rows = (
                await session.execute(select(SchedulerJobSetting))
            ).scalars().all()
            protected = keep or set()
            orphans = [
                r
                for r in rows
                if r.job_name not in known_jobs and r.job_name not in protected
            ]
            if not orphans:
                return 0
            # 删除前先留快照（不可逆 → 事后能回答「删了什么」）；快照与
            # DELETE 同一事务 → 写不进去就整体回滚，旧行不删
            await _snapshot_settings(session, orphans, "清理已下线作业")
            await session.execute(
                delete(SchedulerJobSetting).where(
                    SchedulerJobSetting.job_name.in_(
                        [r.job_name for r in orphans]
                    )
                )
            )
            logger.info(
                "清理已下线作业的调度配置：%s",
                "、".join(sorted(r.job_name for r in orphans)),
            )
            return len(orphans)
    except Exception as exc:
        logger.warning("清理已下线作业的调度配置失败（可忽略）：%s", exc)
        startup_status.record("job_settings_prune", exc)
        return 0


async def migrate_renamed_job(
    old_name: str,
    new_name: str,
    known_jobs: set[str],
) -> bool:
    """把旧作业名的**启停状态**迁移到新作业名（作业合并/改名时用）。

    例：``scan_cross_seed`` → ``cross_seed_maintenance``（2026-11-01 合并）。
    用户手动**停用过**旧作业 → 这个意图要跟着迁过去。

    **只搬 ``enabled``，不搬 ``cron``**：cron 是**作业级频率**，合并后
    频率语义变了（三个旧作业合成一个，旧 cron 对应的是其中某一个）。
    搬过去会让新作业跑在错误的频率上——用代码里的新默认值（3 小时）。
    用户之后想改 cron，在新作业上改即可。

    目标已存在行 → **不覆盖**（用户可能已经在新作业上改过）。

    Returns:
        是否发生了迁移。
    """
    from sqlalchemy import delete

    try:
        async with session_scope() as session:
            old_row = await session.get(SchedulerJobSetting, old_name)
            if old_row is None:
                return False
            if new_name in known_jobs:
                new_row = await session.get(SchedulerJobSetting, new_name)
                if new_row is None:
                    # 目标没有行 → 只搬启停状态，cron 用代码默认值
                    new_row = SchedulerJobSetting(
                        job_name=new_name,
                        enabled=bool(old_row.enabled),
                        cron=None,
                    )
                    session.add(new_row)
                # 删除前先留快照（不可逆 → 事后能回答「删了什么」）；快照与
                # DELETE 同一事务 → 写不进去就整体回滚，旧行不删
                await _snapshot_settings(session, [old_row], "迁移到新作业")
                await session.execute(
                    delete(SchedulerJobSetting).where(
                        SchedulerJobSetting.job_name == old_name
                    )
                )
                logger.info(
                    "作业配置已迁移：%s → %s（enabled=%s，cron 用新默认值）",
                    old_name,
                    new_name,
                    old_row.enabled,
                )
                return True
            # 新作业不在注册表里 → 不迁移（等它真被注册时再说）
            return False
    except Exception as exc:
        logger.warning("迁移作业配置失败（可忽略）：%s", exc)
        startup_status.record("job_settings_migrate", exc)
        return False
