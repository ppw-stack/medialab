"""归拢引擎：把副数据源上的文件搬回主数据源（services 层）。

## 一句话

副盘是缓冲，主盘是唯一真源。本模块把「在途台账」里的文件**逐个**搬到主盘，
主盘确认落盘后**自动清理副盘副本**。

## 链路（与手动迁移同一条，见 :mod:`app.services.migration_pipeline`）

```
取直链 → 提交云下载（光鸭 → 光鸭，服务端内部复制）→ 等任务结束
→ 改名（落盘名是 download）→ 校验同名同大小 → 清理副盘副本
```

## 三条硬约束（实测结论，不是偏好）

1. **云添加额度 3 次/账号/天，失败也扣** → **撞墙探测**（服务端不提供剩余
   次数查询）：撞到「次数超限」类文案 → 整组转 ``paused``（不硬撞不重试），
   次日额度窗口打开时自动续传。
2. **恒串行**（并发 1）——落盘名撞车（``download(1)``）+ 额度都是瓶颈。
3. **副盘副本只在主盘确认落盘后清理**——顺序不可反：先删源再传，
   一旦传失败就是数据没了。

## 中断恢复

- ``syncing`` 的行：先查光鸭活跃任务列表，任务**还在跑** → 本轮跳过；
  任务**已消失** → 重置为待归拢（若已落盘没改名，认领残留那一步会接住）
- 上一轮「已落盘但没改名」的 ``download`` 文件 → 按字节数认领并改名
  （**不重复下载**——重复提交就是白扣额度）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.adapters.drivers.base import DriverError
from app.core.logging import get_logger
from app.core.timeutils import bj_now
from app.db.models import AggregateGroup, Driver, SecondaryStaging
from app.db.session import session_scope
from app.domain.aggregate_types import (
    ROLE_MASTER,
    ROLE_SECONDARY,
    STAGING_DONE,
    STAGING_FAILED,
    STAGING_PAUSED,
    STAGING_PENDING,
    STAGING_SYNCING,
    STAGING_VERIFIED,
)
from app.services.driver_factory import build_driver
from app.services.errors import ServiceError
from app.services.migration import persist_guangya_credential
from app.services.migration_pipeline import (
    GUANGYA_LANDED_NAME,
    VERIFY_DELAY_SEC,
    VERIFY_RETRIES,
    SubmitGate,
    TargetIndex,
    TaskWatcher,
)

logger = get_logger(__name__)

__all__ = ["AggregateSyncResult", "run_aggregate_sync", "sync_group_now"]

#: 光鸭源的提交间隔（秒）——云添加额度稀缺，**慢一点比撞限额好**。
#:
#: 与手动迁移同款（光鸭源收紧到 5s），但这里**恒为 5s**：归拢是后台任务，
#: 不赶时间，撞限额才是真损失。
SUBMIT_INTERVAL_SEC = 5.0

#: 同一轮的**最大提交数**——**纯运行时保护**（一轮跑太久会占着事件循环不放）。
#:
#: ⚠️ **它不是额度限制**：额度由**服务端**说了算（撞墙即停），这里只防单轮
#: 跑飞。原先按「3 次/天」设成 3 是因为当时有额度账本；改成撞墙探测后
#: 放开到 20——账号实际额度更大时（如 VIP 1000 次/天）能用满。
MAX_SUBMISSIONS_PER_RUN = 20

#: ⚠️ **失败的文件不自动重试**（2026-11-15 决策）
#:
#: 光鸭撞额度时**可能只回泛化的「云添加失败」**（无「次数超限」字样），
#: 认不出是额度问题；而**失败也扣额度** → 自动重试就是白扣，且永远等不到
#: 「自动续传」。故泛化失败一律记 ``failed``，**只由人工点「重试」**。
#:
#: 注意：**暂停**（``paused``，文案明确是额度耗尽）**仍会自动续传**——
#: 次日额度窗口打开，作业把 ``paused`` 行捡起来继续。

#: 云添加额度耗尽的文案特征（命中即暂停，不硬撞不重试）。
QUOTA_ERROR_MARKERS = ("次数超限", "额度", "配额", "每日上限")


@dataclass(slots=True)
class AggregateSyncResult:
    """一轮归拢的结果（作业返回值 / API 响应）。"""

    groups: int = 0
    #: 处理成功的文件数
    synced: int = 0
    #: 已清理副盘副本的文件数
    cleaned: int = 0
    #: 因额度耗尽暂停的文件数
    paused: int = 0
    #: 失败的文件数
    failed: int = 0
    #: 跳过的文件数（已在途 / 认领了残留）
    skipped: int = 0
    #: 逐条明细（文件名 → 结果说明）
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": self.groups,
            "synced": self.synced,
            "cleaned": self.cleaned,
            "paused": self.paused,
            "failed": self.failed,
            "skipped": self.skipped,
            "details": list(self.details),
        }


class _QuotaExceeded(Exception):
    """云添加额度耗尽——本组转 ``paused``（当日不恢复，次日续传）。"""


def _is_quota_error(message: str) -> bool:
    """错误文案里是否含「额度耗尽」特征。"""
    text = str(message or "")
    return any(marker in text for marker in QUOTA_ERROR_MARKERS)


def _landed_name_from_url(url: str) -> str:
    """从直链 URL 推导云下载的落盘名（取路径末段）。

    光鸭云下载按 URL 末段命名——光鸭 CDN 直链末段是 ``download``（实测）。
    """
    if not url:
        return ""
    path = str(url).split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""


def _landed_candidates(existing: dict[str, int], url: str = "") -> tuple[str, ...]:
    """可被认领 / 改名的落盘名候选（**排除目录里本来就有的文件**）。

    云下载落盘名取自 URL 末段（光鸭直链 = ``download``），撞名时服务端会
    自动改成 ``download(1)``（:func:`app.services.migration_pipeline.TargetIndex.
    rename_landed` 已支持该形态）。

    ⚠️ **必须排除目标目录里原有的同名文件**——用户自己有个叫 ``download``
    的文件、且字节数恰好与落地文件一致时，改名会**误改用户的文件**
    （改名不可逆）。``existing`` 是提交**前**的目录快照，故不含本次落地的
    文件，但含用户原有的文件——正是要排除的那些。
    """
    names = {GUANGYA_LANDED_NAME}
    derived = _landed_name_from_url(url)
    if derived:
        names.add(derived)
    return tuple(sorted(name for name in names if name and name not in existing))


def _refresh_callback(driver_id: int) -> Any:
    async def _cb(cred: Any) -> None:
        await persist_guangya_credential(driver_id, cred)

    return _cb


# --------------------------------------------------------------------------
# 作业入口
# --------------------------------------------------------------------------


async def run_aggregate_sync() -> dict[str, Any]:
    """跑一轮归拢（调度作业入口）。

    **组间串行**：光鸭的限速是账号级，组间并发并不能提高总吞吐，
    反而让单次作业的耗时不可预测。组内更必须串行（落盘名撞车）。
    """
    result = AggregateSyncResult()
    async with session_scope() as db:
        groups = (
            (await db.execute(select(AggregateGroup).order_by(AggregateGroup.id)))
            .scalars()
            .all()
        )
        group_ids = [(int(g.id), g.name, bool(g.enabled)) for g in groups]

    for group_id, group_name, enabled in group_ids:
        if not enabled:
            continue
        result.groups += 1
        try:
            await _sync_group(group_id, result)
        except Exception as exc:  # 单组失败不影响其它组
            logger.exception("聚合组 %s 归拢失败", group_name)
            result.failed += 1
            result.details.append(f"组「{group_name}」失败：{exc}")
    return result.to_dict()


async def _sync_group(group_id: int, result: AggregateSyncResult) -> None:
    """归拢一个聚合组。"""
    async with session_scope() as db:
        group = (
            await db.execute(
                select(AggregateGroup).where(AggregateGroup.id == group_id)
            )
        ).scalar_one_or_none()
        if group is None:
            return
        master = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.aggregate_group_id == group_id,
                        Driver.role == ROLE_MASTER,
                    )
                )
            )
            .scalars()
            .first()
        )
        if master is None:
            logger.info("聚合组 %s 没有主数据源，跳过", group.name)
            return
        if not master.enabled or not master.media_cid:
            logger.info(
                "聚合组 %s 的主数据源未启用或未配置媒体库目录，跳过",
                group.name,
            )
            return
        master_id = int(master.id)
        master_credential = master.credential
        master_device_type = master.device_type
        root_id = str(master.media_cid)
        group_name = group.name

    rows = await _load_processable_rows(group_id)
    if not rows:
        return

    master_driver = build_driver(
        "guangya",
        master_credential,
        master_device_type,
        on_token_refresh=_refresh_callback(master_id),
    )
    index = TargetIndex(master_driver, root_id)
    watcher = TaskWatcher(master_driver, log=logger.info)
    gate = SubmitGate(SUBMIT_INTERVAL_SEC)
    submissions = 0

    try:
        for row in rows:
            if submissions >= MAX_SUBMISSIONS_PER_RUN:
                result.details.append(
                    f"组「{group_name}」：本轮已达最大提交数（{MAX_SUBMISSIONS_PER_RUN}），"
                    "其余留到下一轮"
                )
                break
            source = await _build_source_driver(row)
            try:
                need_submit = await _sync_one(
                    row,
                    master_driver=master_driver,
                    source=source,
                    index=index,
                    watcher=watcher,
                    gate=gate,
                    result=result,
                    group_name=group_name,
                )
                if need_submit:
                    submissions += 1
            except _QuotaExceeded as exc:
                # **撞墙 = 当天的门关了** → 整组剩余行一次转暂停 + 停手，
                # 不再「一行撞一次」（每次撞墙都是一次白费的服务端请求）。
                # 暂停的行**次日自动续传**（作业把 paused 行捡起来继续）。
                paused = await _pause_group(group_id, str(exc))
                result.paused += paused
                result.details.append(
                    f"组「{group_name}」：{exc}（剩余 {paused} 个文件转暂停，次日自动续传）"
                )
                break
            except Exception as exc:
                await _mark(row.id, STAGING_FAILED, error=f"{type(exc).__name__}: {exc}")
                result.failed += 1
                result.details.append(f"{row.file_name}：失败 {exc}")
                logger.warning("归拢失败 %s：%s", row.file_name, exc)
    finally:
        await watcher.close()


# --------------------------------------------------------------------------
# 单文件归拢
# --------------------------------------------------------------------------


async def _sync_one(
    row: _Row,
    *,
    master_driver: Any,
    source: Any,
    index: TargetIndex,
    watcher: TaskWatcher,
    gate: SubmitGate,
    result: AggregateSyncResult,
    group_name: str,
) -> bool:
    """归拢一个文件。返回是否真的提交了云下载（用于统计本轮提交数）。"""
    await _mark(row.id, STAGING_SYNCING, error=None, bump_attempts=True)

    # ① 目标目录 + 预检
    target_dir_id, existing = await index.resolve(row.target_dir)
    remote_size = existing.get(row.file_name)
    if remote_size is not None:
        if remote_size == row.size_bytes:
            # 主盘已有同名同大小 → 副盘那份是冗余副本
            await _cleanup_source(row, source, result)
            result.skipped += 1
            result.details.append(f"{row.file_name}：主盘已有同名同大小，直接清理副盘")
            return False
        if remote_size > row.size_bytes:
            # 保留大的（主盘那份更大）
            await _cleanup_source(row, source, result)
            result.skipped += 1
            result.details.append(f"{row.file_name}：主盘已有更大版本，直接清理副盘")
            return False
        # 源更大 → 删主盘侧的小文件，然后正常传（保留大的）
        removed = await index.delete_replaced(
            row.target_dir, row.file_name, remote_size
        )
        if not removed:
            raise DriverError(
                "主盘同名文件已变化，未覆盖（不误删）", kind="unknown"
            )

    # ② 认领残留：上一轮已落盘但没改名（中断 / 被取消）
    #
    # ⚠️ **候选落盘名要排除「目录里本来就有的文件」**——用户自己有个叫
    # ``download`` 的文件且字节数恰好一致时，改名会误改它（不可逆）。
    # ``existing`` 是**提交前**的目录快照，故不会含本次落地的文件。
    adopted = await index.rename_landed(
        row.target_dir,
        _landed_candidates(existing),
        row.size_bytes,
        row.file_name,
    )
    if adopted:
        await _cleanup_source(row, source, result)
        result.skipped += 1
        result.details.append(f"{row.file_name}：认领上一轮残留（已落盘未改名）")
        return False

    # ③ 取直链（光鸭直链不绑 UA）
    link = await source.get_direct_link(row.remote_id, "")
    if not link.url:
        raise DriverError("副盘返回空直链", kind="not_found")

    # ④ 提交间隔（错开提交，别把光鸭打爆）
    waited = await gate.wait()
    if waited >= 1:
        logger.info("归拢错开提交，等待 %.0fs：%s", waited, row.file_name)

    # ⑤ 提交云下载
    #
    # ⚠️ **额度靠撞墙判定**（没有账本——服务端不提供「剩余次数」查询）：
    # 撞到「次数超限 / 额度 / 配额 / 每日上限」字样 → 抛 _QuotaExceeded →
    # 整组剩余行转暂停，**次日自动续传**。
    # 泛化失败（「云添加失败」code=8）**认不出是不是额度** → 当普通失败，
    # 记 ``failed`` 且**不自动重试**（失败也扣额度，自动重试就是白扣）。
    try:
        task_id = await index.create_offline_task(link.url, target_dir_id)
    except DriverError as exc:
        if _is_quota_error(str(exc)):
            raise _QuotaExceeded(f"云添加额度耗尽：{exc}") from exc
        raise
    await index.note_submitted(row.target_dir, row.file_name, row.size_bytes)
    await _mark(row.id, STAGING_SYNCING, task_id=task_id, error=None)

    # ⑥ 等任务结束（从活跃列表消失）
    await watcher.wait(task_id, path=row.file_name, size=row.size_bytes)

    # ⑦ 改名（落盘名是 URL 末段 = download）
    landed_names = _landed_candidates(existing, link.url)
    renamed = await index.rename_landed(
        row.target_dir,
        landed_names,
        row.size_bytes,
        row.file_name,
    )
    if renamed:
        logger.info("归拢改名：%s → %s", renamed, row.file_name)

    # ⑧ 校验：主盘目录里是否真的出现同名同大小文件
    for attempt in range(VERIFY_RETRIES):
        files = await index.refresh(row.target_dir)
        if files.get(row.file_name) == row.size_bytes:
            await _cleanup_source(row, source, result)
            result.synced += 1
            result.details.append(f"{row.file_name}：已归拢")
            return True
        if attempt + 1 < VERIFY_RETRIES:
            await asyncio.sleep(VERIFY_DELAY_SEC)

    reason = await index.failure_reason(task_id)
    if _is_quota_error(reason):
        raise _QuotaExceeded(f"云添加额度耗尽：{reason}")
    raise DriverError(
        reason or "云下载已结束，但主盘目录未出现同名同大小文件", kind="unknown"
    )


async def _cleanup_source(
    row: _Row, source: Any, result: AggregateSyncResult
) -> None:
    """清理副盘副本（**只在主盘确认落盘同名同大小之后**）。

    - 走 ``fs_delete`` → **进回收站**，不是永久删除（可恢复）
    - **记审计日志**——删数据不可逆，必须能回答「删了什么」
    - 单个失败不中断其余条目（只记警告，状态留 ``verified`` 等下轮重试）
    """
    try:
        await source.delete_file(row.remote_id)
    except Exception as exc:
        logger.warning("清理副盘副本失败 %s：%s", row.file_name, exc)
        await _mark(row.id, STAGING_VERIFIED, error=f"清理副盘副本失败：{exc}")
        return
    await _mark(row.id, STAGING_DONE, error=None, cleaned=True)
    result.cleaned += 1
    logger.warning(
        "聚合归拢已清理副盘副本：删除 %s（%d 字节）", row.file_name, row.size_bytes
    )


# --------------------------------------------------------------------------
# 台账读写
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    """台账行的**脱离 ORM 的快照**（会话关闭后仍可安全读取）。"""

    id: int
    group_id: int
    source_driver_id: int
    target_driver_id: int
    remote_id: str
    file_name: str
    source_dir: str
    target_dir: str
    size_bytes: int
    state: str
    attempts: int
    last_error: str | None
    cloud_task_id: str | None


async def _load_processable_rows(group_id: int) -> list[_Row]:
    """取本轮要处理的行（``pending`` / ``paused`` / ``failed`` / ``syncing``）。

    - ``paused``（额度耗尽，**文案明确**）→ 次日额度窗口打开后自动续传
    - ``failed``（泛化失败，认不出是不是额度）→ **不自动重试**，只由人工点
      「重试」——失败也扣额度，自动重试就是白扣
    - ``syncing`` → 先看光鸭活跃任务：**还在跑就跳过**（别重复提交），
      已消失则重置为 ``pending``（残留认领会接住已落盘的情况）
    """
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(SecondaryStaging)
                    .where(
                        SecondaryStaging.group_id == group_id,
                        SecondaryStaging.state.notin_((STAGING_DONE,)),
                    )
                    .order_by(SecondaryStaging.id)
                )
            )
            .scalars()
            .all()
        )
        detached = [
            _Row(
                id=int(r.id),
                group_id=int(r.group_id),
                source_driver_id=int(r.source_driver_id),
                target_driver_id=int(r.target_driver_id),
                remote_id=str(r.remote_id),
                file_name=str(r.file_name),
                source_dir=str(r.source_dir),
                target_dir=str(r.target_dir),
                size_bytes=int(r.size_bytes),
                state=str(r.state),
                attempts=int(r.attempts),
                last_error=r.last_error,
                cloud_task_id=r.cloud_task_id,
            )
            for r in rows
        ]

    active_ids = await _active_task_ids(detached)
    result: list[_Row] = []
    for row in detached:
        if row.state == STAGING_SYNCING:
            if row.cloud_task_id and row.cloud_task_id in active_ids:
                continue  # 云下载还在跑 → 本轮不碰
            # 任务已消失（可能已落盘）→ 重置为待归拢，残留认领会接住
            await _mark(row.id, STAGING_PENDING, error=None, task_id="")
            row.state = STAGING_PENDING
        if row.state == STAGING_FAILED:
            # ⚠️ **失败不自动重试**（2026-11-15 决策）——泛化失败认不出是不是
            # 额度问题，而**失败也扣额度** → 自动重试就是白扣。只由人工点
            # 「重试」（:func:`app.services.aggregate_staging.retry_staging`）。
            continue
        result.append(row)
    return result


async def _active_task_ids(rows: list[_Row]) -> set[str]:
    """查光鸭在途任务 id 集合（失败返回空集——不阻塞本轮）。"""
    ids = {r.cloud_task_id for r in rows if r.cloud_task_id}
    if not ids:
        return set()
    async with session_scope() as db:
        driver_row = (
            (
                await db.execute(
                    select(Driver).where(
                        Driver.id == rows[0].target_driver_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if driver_row is None:
            return set()
        credential = driver_row.credential
        device_type = driver_row.device_type
        driver_id = int(driver_row.id)

    driver = build_driver(
        "guangya",
        credential,
        device_type,
        on_token_refresh=_refresh_callback(driver_id),
    )
    try:
        items = await driver.list_active_offline_tasks()  # type: ignore[attr-defined]
    except Exception as exc:
        logger.warning("读取在途任务失败（不做中断去重）：%s", exc)
        return set()
    return {str(item.get("taskId") or "") for item in items}


async def _pause_group(group_id: int, reason: str) -> int:
    """把组内所有待归拢行转 ``paused``（撞上额度墙）。返回转成功的条数。

    含 ``syncing``——撞墙时**当前行**正处在 syncing（提交被拒），它也得
    一起暂停，否则下次作业会把它当「在途任务已消失」重新捡起来。
    """
    async with session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(SecondaryStaging).where(
                        SecondaryStaging.group_id == group_id,
                        SecondaryStaging.state.in_((STAGING_PENDING, STAGING_SYNCING)),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.state = STAGING_PAUSED
            row.last_error = reason
            row.updated_at = bj_now()
        await db.commit()
        return len(rows)


async def _mark(
    staging_id: int,
    state: str,
    *,
    error: str | None = None,
    task_id: str | None = None,
    cleaned: bool = False,
    bump_attempts: bool = False,
) -> None:
    """更新台账行状态（**短事务**，不跨网络 IO）。"""
    async with session_scope() as db:
        row = (
            await db.execute(
                select(SecondaryStaging).where(SecondaryStaging.id == staging_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.state = state
        row.last_error = error
        if task_id is not None:
            row.cloud_task_id = task_id or None
        if bump_attempts:
            row.attempts = int(row.attempts) + 1
        if state == STAGING_VERIFIED:
            row.synced_at = bj_now()
        if cleaned:
            row.cleaned_at = bj_now()
        row.updated_at = bj_now()
        await db.commit()


async def _build_source_driver(row: _Row) -> Any:
    """构造副盘驱动（光鸭）。"""
    async with session_scope() as db:
        driver_row = (
            (
                await db.execute(
                    select(Driver).where(Driver.id == row.source_driver_id)
                )
            )
            .scalars()
            .first()
        )
        if driver_row is None:
            raise ServiceError("副数据源不存在", status=404)
        if driver_row.role != ROLE_SECONDARY:
            raise ServiceError(
                f"存储源「{driver_row.name}」已不是副数据源", status=400
            )
        credential = driver_row.credential
        device_type = driver_row.device_type
        driver_id = int(driver_row.id)
    return build_driver(
        "guangya",
        credential,
        device_type,
        on_token_refresh=_refresh_callback(driver_id),
    )


# --------------------------------------------------------------------------
# 手动触发（API）
# --------------------------------------------------------------------------


async def sync_group_now(group_id: int) -> AggregateSyncResult:
    """手动跑一轮某个组的归拢（API 触发）。"""
    result = AggregateSyncResult(groups=1)
    await _sync_group(group_id, result)
    return result
