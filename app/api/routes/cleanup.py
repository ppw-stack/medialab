"""清理中心 API（FR-6.1 清理中心 + FR-5.1 / FR-5.2 / FR-5.4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.models import (
    CleanupConfirmation,
    CleanupLog,
    CleanupPolicy,
    CleanupRule,
    CleanupStatus,
    CleanupTrigger,
)
from app.db.session import session_scope
from app.services.abort_bridge import execute_pending_aborts
from app.services.cleanup import Cleaner, generate_report
from app.services.factory import get_factory

router = APIRouter(prefix="/cleanup", tags=["cleanup"])


# ---------------------------------------------------------------- 策略
class CleanupPolicyOut(BaseModel):
    dry_run_on_first_use: bool
    batch_limit: int
    space_limit: int
    protect_hours: int
    trash_retention_days: int
    low_water: int
    high_water: int
    default_sources: str


class CleanupPolicyIn(BaseModel):
    dry_run_on_first_use: bool | None = None
    batch_limit: int | None = Field(default=None, ge=1, le=10000)
    space_limit: int | None = Field(default=None, ge=1)
    protect_hours: int | None = Field(default=None, ge=0, le=168)
    trash_retention_days: int | None = Field(default=None, ge=1, le=365)
    low_water: int | None = Field(default=None, ge=1)
    high_water: int | None = Field(default=None, ge=1)
    default_sources: str | None = None


@router.get("/policy", summary="清理策略（FR-5.2）")
async def get_policy() -> CleanupPolicyOut:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(CleanupPolicy).where(CleanupPolicy.id == 1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = CleanupPolicy(id=1)
            session.add(row)
            await session.flush()
        return CleanupPolicyOut(
            dry_run_on_first_use=row.dry_run_on_first_use,
            batch_limit=row.batch_limit,
            space_limit=row.space_limit,
            protect_hours=row.protect_hours,
            trash_retention_days=row.trash_retention_days,
            low_water=row.low_water,
            high_water=row.high_water,
            default_sources=row.default_sources,
        )


@router.put("/policy", summary="修改清理策略")
async def update_policy(payload: CleanupPolicyIn) -> CleanupPolicyOut:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(CleanupPolicy).where(CleanupPolicy.id == 1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = CleanupPolicy(id=1)
            session.add(row)
        for key, value in payload.model_dump(exclude_unset=True).items():
            if value is not None:
                setattr(row, key, value)
    return await get_policy()


# ---------------------------------------------------------------- 规则
class RuleIn(BaseModel):
    name: str
    when: dict[str, Any] = Field(
        default_factory=dict,
        description="DSL 条件：ratio_gte / seeding_days_gte / size_gte / status / category_eq 等",
    )
    enabled: bool = True
    priority: int = 100


class RuleOut(BaseModel):
    id: int
    name: str
    enabled: bool
    priority: int
    when: dict[str, Any]
    dry_run_done: bool
    created_at: datetime


@router.get("/rules", summary="清理规则列表")
async def list_rules() -> list[RuleOut]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(CleanupRule).order_by(
                        CleanupRule.priority, CleanupRule.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            RuleOut(
                id=r.id,
                name=r.name,
                enabled=r.enabled,
                priority=r.priority,
                when=dict(r.when or {}),
                dry_run_done=r.dry_run_done,
                created_at=r.created_at,
            )
            for r in rows
        ]


@router.post("/rules", status_code=201, summary="新增规则")
async def create_rule(payload: RuleIn) -> RuleOut:
    async with session_scope() as session:
        exists = (
            await session.execute(
                select(CleanupRule.id).where(CleanupRule.name == payload.name)
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="规则名已存在")
        rule = CleanupRule(
            name=payload.name,
            when=payload.when,
            enabled=payload.enabled,
            priority=payload.priority,
        )
        session.add(rule)
        await session.flush()
        return RuleOut(
            id=rule.id,
            name=rule.name,
            enabled=rule.enabled,
            priority=rule.priority,
            when=dict(rule.when or {}),
            dry_run_done=rule.dry_run_done,
            created_at=rule.created_at,
        )


@router.put("/rules/{rule_id}", summary="修改规则")
async def update_rule(rule_id: int, payload: RuleIn) -> RuleOut:
    async with session_scope() as session:
        rule = (
            await session.execute(
                select(CleanupRule).where(CleanupRule.id == rule_id)
            )
        ).scalar_one_or_none()
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        rule.name = payload.name
        rule.when = payload.when
        rule.enabled = payload.enabled
        rule.priority = payload.priority
        # 修改后必须重跑干跑（配置变了，行为不可预测）
        rule.dry_run_done = False
        return RuleOut(
            id=rule.id,
            name=rule.name,
            enabled=rule.enabled,
            priority=rule.priority,
            when=dict(rule.when or {}),
            dry_run_done=rule.dry_run_done,
            created_at=rule.created_at,
        )


@router.delete("/rules/{rule_id}", summary="删除规则")
async def delete_rule(rule_id: int) -> dict[str, bool]:
    async with session_scope() as session:
        rule = (
            await session.execute(
                select(CleanupRule).where(CleanupRule.id == rule_id)
            )
        ).scalar_one_or_none()
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        await session.delete(rule)
        return {"deleted": True}


class DryRunResult(BaseModel):
    rule_id: int
    rule_name: str
    matched: int
    by_reason: dict[str, int] = {}


@router.post("/rules/{rule_id}/dry-run", summary="FR-5.2.2 干跑预览")
async def dry_run_rule(rule_id: int) -> DryRunResult:
    """列出将被该规则命中的任务，**不执行任何删除**。

    新规则首次启用强制走此端点；执行端点会再做干跑门校验。
    """
    async with session_scope() as session:
        rule = (
            await session.execute(
                select(CleanupRule).where(CleanupRule.id == rule_id)
            )
        ).scalar_one_or_none()
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        factory = get_factory()
        cleaner = Cleaner(session, factory)
        out = await cleaner.run_rule(rule, dry_run=True)
    return DryRunResult(
        rule_id=rule_id,
        rule_name=rule.name,
        matched=out.candidates_total,
        by_reason=out.blocked_reasons,
    )


class RunResult(BaseModel):
    rule_id: int
    candidates_total: int
    candidates_blocked: int
    candidates_executed: int
    freed_space: int
    pending_confirmation: int
    blocked_reasons: dict[str, int]
    error: str = ""


@router.post("/rules/{rule_id}/run", summary="实际执行规则")
async def run_rule(rule_id: int, trigger: str = CleanupTrigger.REPORT) -> RunResult:
    async with session_scope() as session:
        rule = (
            await session.execute(
                select(CleanupRule).where(CleanupRule.id == rule_id)
            )
        ).scalar_one_or_none()
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        factory = get_factory()
        cleaner = Cleaner(session, factory)
        out = await cleaner.run_rule(rule, trigger_source=trigger)
    return RunResult(
        rule_id=rule_id,
        candidates_total=out.candidates_total,
        candidates_blocked=out.candidates_blocked,
        candidates_executed=out.candidates_executed,
        freed_space=out.freed_space,
        pending_confirmation=out.pending_confirmation,
        blocked_reasons=out.blocked_reasons,
        error=out.error,
    )


# ---------------------------------------------------------------- 二次确认
class ConfirmationOut(BaseModel):
    id: int
    rule_id: int | None
    rule_name: str
    trigger_source: str
    affected_count: int
    estimated_space: int
    download_root: str
    status: str
    created_at: datetime
    expires_at: datetime | None


@router.get("/confirmations", summary="FR-5.2.4 二次确认队列")
async def list_confirmations() -> list[ConfirmationOut]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(CleanupConfirmation)
                    .where(CleanupConfirmation.status == CleanupStatus.PENDING)
                    .order_by(CleanupConfirmation.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            ConfirmationOut(
                id=r.id,
                rule_id=r.rule_id,
                rule_name=r.rule_name,
                trigger_source=r.trigger_source,
                affected_count=r.affected_count,
                estimated_space=r.estimated_space,
                download_root=r.download_root,
                status=r.status,
                created_at=r.created_at,
                expires_at=r.expires_at,
            )
            for r in rows
        ]


@router.post("/confirmations/{confirmation_id}/confirm", summary="确认执行")
async def confirm(confirmation_id: int) -> dict[str, str]:
    """用户点击确认后实际执行清理（FR-5.2.4）。"""
    async with session_scope() as session:
        conf = (
            await session.execute(
                select(CleanupConfirmation).where(
                    CleanupConfirmation.id == confirmation_id
                )
            )
        ).scalar_one_or_none()
        if conf is None:
            raise HTTPException(status_code=404, detail="确认任务不存在")
        if conf.status != CleanupStatus.PENDING:
            raise HTTPException(status_code=409, detail=f"状态 {conf.status} 不可确认")

        # 真实执行：直接走 Cleaner 流水线但绕过二次确认门
        factory = get_factory()
        cleaner = Cleaner(session, factory)

        # 通过临时降低阈值，让确认直接执行
        policy = await cleaner._load_policy()
        original_batch, original_space = policy.batch_limit, policy.space_limit
        policy.batch_limit = max(original_batch, conf.affected_count + 1)
        policy.space_limit = max(original_space, conf.estimated_space + 1)
        try:
            # 构造一个临时规则，复用 run_rule 管线
            rule = CleanupRule(
                id=conf.rule_id or 0,
                name=conf.rule_name + " [confirmed]",
                enabled=True,
                priority=999,
                when={},
            )
            out = await cleaner.run_rule(rule, trigger_source=conf.trigger_source)
            conf.status = CleanupStatus.DONE if out.error == "" else CleanupStatus.FAILED
        finally:
            policy.batch_limit = original_batch
            policy.space_limit = original_space

    return {"id": str(confirmation_id), "status": conf.status}


@router.post("/confirmations/{confirmation_id}/cancel", summary="取消确认")
async def cancel_confirmation(confirmation_id: int) -> dict[str, str]:
    async with session_scope() as session:
        conf = (
            await session.execute(
                select(CleanupConfirmation).where(
                    CleanupConfirmation.id == confirmation_id
                )
            )
        ).scalar_one_or_none()
        if conf is None:
            raise HTTPException(status_code=404, detail="确认任务不存在")
        conf.status = CleanupStatus.CANCELLED
    return {"id": str(confirmation_id), "status": CleanupStatus.CANCELLED}


# ---------------------------------------------------------------- 报告
@router.get("/report", summary="FR-5.4 清理报告")
async def report() -> dict[str, Any]:
    async with session_scope() as session:
        return await generate_report(session)


# ---------------------------------------------------------------- 中止
@router.post("/execute-aborts", summary="执行所有 pending 的中止任务")
async def execute_aborts() -> RunResult:
    """M4 落库的中止产物（task_queue 中 status=pending 的 abort_window）一次性执行。

    与调度作业 `cleanup_aborts` 触发同一条路径。
    """
    factory = get_factory()
    out = await execute_pending_aborts(factory)
    return RunResult(
        rule_id=0,
        candidates_total=out.candidates_total,
        candidates_blocked=out.candidates_blocked,
        candidates_executed=out.candidates_executed,
        freed_space=out.freed_space,
        pending_confirmation=out.pending_confirmation,
        blocked_reasons=out.blocked_reasons,
        error=out.error,
    )


# ---------------------------------------------------------------- 审计
@router.get("/logs", summary="清理审计日志（FR-5.2.7）")
async def list_logs(limit: int = 100) -> list[dict[str, Any]]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(CleanupLog).order_by(CleanupLog.ts.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "ts": r.ts,
                "rule_name": r.rule_name,
                "action": r.action,
                "trigger_source": r.trigger_source,
                "affected_count": r.affected_count,
                "freed_space": r.freed_space,
                "result": r.result,
                "error": r.error,
            }
            for r in rows
        ]
