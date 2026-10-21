"""站点每日签到服务（FR-3.6 站点签到契约）。

``daily_checkin`` 周期性作业（cron ``37 8 * * *``）：

1. 读取所有 ``Site.enabled = 1`` 的站点
2. 按站拿到对应 :class:`SiteAdapter` 实例
3. 调单方法 :meth:`SiteAdapter.checkin`
4. 单站失败不阻塞其他站（异常隔离）
5. 结果**不落库**（不建表）——作为作业返回值进 ``job_runs`` 表

.. note::

   **不建表**是刻意的决策：签到是每天一次的低频事件，``job_runs`` 已经
   记录了每次作业的完整结果（``result_json`` 里有 ``summary`` + ``steps``），
   前端「调度 → 历史」直接能看。再建一张表存同样的信息是重复。

**网络 IO 在事务外**：与 :mod:`app.services.user_data` 同样的约定——
写锁不能横跨网络往返（会与 cron 拉取作业争抢 SQLite 写锁，
引发 ``database is locked``）。

**失败必须计数**：2026-10-21 prod 事故的教训——拉取失败被吞成空结果，
作业统计报「全部成功」而实际漏掉了故障站。故签到失败必须计入 ``failed``
并点名到 summary 里。
"""

from __future__ import annotations

from sqlalchemy import select

from app.adapters.sites.base import BreakerTripped, CheckinStatus
from app.core.logging import get_logger
from app.db.models import Site
from app.db.session import get_session_factory, session_scope
from app.services.factory import AdapterFactory
from app.services.scheduler import JobStep

logger = get_logger(__name__)

__all__ = ["refresh_all_sites"]


async def refresh_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """遍历所有启用站点执行每日签到。

    Returns: ``{"total": N, "success": N, "already": N, "failed": N,
    "unsupported": N, "summary": "...", "steps": [...]}``。

    单站失败不阻塞其他站（异常隔离）；失败站计入 ``failed`` 并在
    summary 里点名——**不可吞掉**（参见模块 docstring 的事故说明）。
    """
    factory_obj = get_session_factory()
    # 短事务读站点列表，脱离 session 后只用标量字段
    async with factory_obj() as session:
        site_rows = (await session.execute(
            select(Site).where(Site.enabled.is_(True))
        )).scalars().all()
        sites = [(r.id, r.name) for r in site_rows]

    total = len(sites)
    success = 0
    already = 0
    failed = 0
    unsupported = 0
    auth_expired = 0
    #: 失败站明细（站名 → 原因）
    failures: list[tuple[str, str]] = []
    #: 需要人工签到的站（人机验证 / 无此功能）
    manual: list[str] = []

    for site_id, site_name in sites:
        try:
            # 短事务读站点行（拿完整 ORM 实例供 factory.site()），
            # 网络 IO 在事务外——见模块 docstring。
            async with session_scope() as session:
                site_row = (await session.execute(
                    select(Site).where(Site.id == site_id)
                )).scalar_one_or_none()
                if site_row is None:
                    continue
                adapter = factory.site(site_row)

            result = await adapter.checkin()
        except BreakerTripped as exc:
            # 熔断：签到是低频作业（每天 1 次），**不写** sites.breaker_until
            # ——不与 10 分钟一次的拉取作业共享熔断状态（避免一次签到
            # 限流把整站的种子拉取也停掉）。
            failed += 1
            reason = f"触发熔断：{exc}"
            failures.append((site_name, reason))
            logger.warning("站点 %s 签到触发熔断：%s", site_name, exc)
            continue
        except Exception as exc:
            failed += 1
            reason = str(exc)
            failures.append((site_name, reason))
            logger.warning("站点 %s 签到失败：%s", site_name, exc)
            continue

        status = result.status
        if status == CheckinStatus.SUCCESS:
            success += 1
            logger.info("站点 %s 签到成功：%s", site_name, result.message)
        elif status == CheckinStatus.ALREADY:
            already += 1
            logger.info("站点 %s 今日已签到", site_name)
        elif status == CheckinStatus.UNSUPPORTED:
            unsupported += 1
            manual.append(site_name)
            logger.info("站点 %s 不支持自动签到：%s", site_name, result.message)
        elif status == CheckinStatus.AUTH_EXPIRED:
            # cookie 失效是**故障**（用户得去更新 cookie），计入 failed；
            # 另用 auth_expired 单独标记，便于 summary 里点明原因。
            failed += 1
            auth_expired += 1
            failures.append((site_name, result.message))
            logger.warning("站点 %s 签到：%s", site_name, result.message)
        else:
            failed += 1
            failures.append((site_name, result.message))
            logger.warning("站点 %s 签到失败：%s", site_name, result.message)

    logger.info(
        "daily_checkin 完成：success=%d already=%d failed=%d unsupported=%d total=%d",
        success, already, failed, unsupported, total,
    )
    return {
        "total": total,
        "success": success,
        "already": already,
        "failed": failed,
        "unsupported": unsupported,
        "auth_expired": auth_expired,
        "summary": _build_summary(
            total, success, already, failed, unsupported, auth_expired,
            failures, manual,
        ),
        "steps": _build_steps(
            total, success, already, failed, unsupported, auth_expired,
            failures, manual,
        ),
    }


def _build_summary(
    total: int,
    success: int,
    already: int,
    failed: int,
    unsupported: int,
    auth_expired: int,
    failures: list[tuple[str, str]],
    manual: list[str],
) -> str:
    """人话摘要（前端直接展示，避免前端硬编码字段名）。

    注：``auth_expired`` 是 ``failed`` 的**子集**（cookie 失效也计入失败），
    这里只补充说明原因，不重复计数。
    """
    parts = [f"{total} 站，{success} 签到成功"]
    if already:
        parts.append(f"{already} 已签过")
    if unsupported:
        parts.append(f"{unsupported} 需人工")
    if failed:
        names = ", ".join(name for name, _ in failures)
        parts.append(f"{failed} 失败（{names}）")
        if auth_expired:
            parts.append(f"其中 {auth_expired} 站登录失效")
    return "，".join(parts)


def _build_steps(
    total: int,
    success: int,
    already: int,
    failed: int,
    unsupported: int,
    auth_expired: int,
    failures: list[tuple[str, str]],
    manual: list[str],
) -> list[JobStep]:
    """步骤卡片（前端「步骤卡片」数据源）。"""
    steps: list[JobStep] = [
        JobStep("查询启用站点", "ok", total, f"{total} 站"),
    ]

    if failed:
        detail = f"{success} 成功，{already} 已签过，{failed} 失败"
        if auth_expired:
            detail += f"（{auth_expired} 站登录失效）"
        steps.append(JobStep("执行签到", "warn", success + already, detail))
    elif success or already:
        steps.append(
            JobStep(
                "执行签到",
                "ok",
                success + already,
                f"{success} 成功，{already} 已签过",
            )
        )
    else:
        steps.append(JobStep("执行签到", "skip", 0, "无可签到的站点"))

    if manual:
        steps.append(
            JobStep(
                "需人工签到",
                "skip",
                len(manual),
                f"{', '.join(sorted(manual))}（人机验证或无签到功能）",
            )
        )
    if failures:
        steps.append(
            JobStep(
                "失败站点",
                "fail",
                len(failures),
                "；".join(f"{name}: {reason}" for name, reason in failures)[:200],
            )
        )
    return steps
