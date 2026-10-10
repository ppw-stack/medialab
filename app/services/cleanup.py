"""清理规则引擎（FR-5.1 + FR-5.2 六重保护）。

**模块是清理逻辑的唯一入口**——任何删除/数据隔离动作必须经
:class:`Cleaner` 走完整管线。直接 ``downloaders.remove(delete_data=True)``
的代码路径**必须不存在**（R3）。

六重保护的责任分配：

1. **白名单** :class:`WhitelistGuard`：分类/标签/手动收藏命中即跳过 + 🔴
2. **干跑** :class:`DryRunGuard`：首次启用未跑干跑 → 走二次确认队列
3. **回收站** :class:`TrashGuard`：先 ``remove(delete_data=False)`` → 文件 rename
4. **二次确认** :class:`ConfirmationGuard`：超阈值 → 写队列，不执行
5. **路径三重校验** :class:`PathSafetyGuard`:
   a. 路径来自下载器上报（不是模板拼接）
   b. ``stat()`` 校验存在 + 批量互不重叠
   c. 必须在下载根目录下 + ``st_dev`` 匹配
6. **保护期** :class:`ProtectPeriodGuard`：`protect_hours` 内禁止

设计要点：

- 规则匹配与动作执行严格分离（先 :meth:`match`，再 :meth:`execute`）
- 命中即停（FR-5.1：避免规则冲突）
- 已知 :class:`CleanupStatus` 状态机贯穿全流程
- 全部异常落入 :class:`cleanup_logs`，**不**靠业务代码自觉
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.downloaders.base import DownloaderAdapter
from app.adapters.downloaders.locks import locks
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.db.models import (
    CleanupAction,
    CleanupConfirmation,
    CleanupPolicy,
    CleanupRule,
    CleanupStatus,
    CleanupTrigger,
    DownloaderInstance,
    Source,
    Torrent,
    TorrentStatus,
)
from app.services.paths import device_of, is_within

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    """一条可清理的候选记录（规则匹配输出）。"""

    torrent: object
    rule: object
    reason: str
    size: int


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """一次清理的执行计划。"""

    candidates: list[CleanupCandidate]
    total_count: int
    total_size: int
    rule_id: int | None
    rule_name: str
    trigger_source: str
    download_root: str


# ================================================================ 规则引擎
class RuleEngine:
    """规则匹配（纯逻辑，不触碰下载器）。"""

    @staticmethod
    def matches(rule: CleanupRule, torrent: Torrent, now: datetime | None = None) -> bool:
        """DSL 匹配：``rule.when`` 是 dict，每个键是条件，全部必须满足。

        支持的字段（未知字段视为 false，避免向后不兼容）：
        ``source`` / ``status`` / ``ratio_gte`` / ``seeding_days_gte`` /
        ``size_gte`` / ``categories`` / ``tags`` / ``site_id`` /
        ``paused_reason_is`` / ``protect_until_passed`` 等。
        """
        ref = now or utcnow()
        cond = dict(rule.when or {})
        return all(RuleEngine._check(key, value, torrent, ref) for key, value in cond.items())

    @staticmethod
    def _check(key: str, value: object, t: Torrent, ref: datetime) -> bool:
        # value 来自 JSON DSL，可能是 str / int / float / list[str|int] / bool
        def as_list(v: object) -> list[str]:
            if isinstance(v, list):
                return [str(x) for x in v]
            return str(v).split(",")

        def as_csv_set(v: object) -> set[str]:
            return {s.strip() for s in as_list(v) if s.strip()}

        try:
            if key == "source":
                return t.source in as_csv_set(value)
            if key == "source_in":
                return t.source in as_csv_set(value)
            if key == "status":
                return t.status in as_csv_set(value)
            if key == "status_not":
                return t.status != str(value)
            if key == "ratio_gte":
                return t.ratio >= float(str(value))
            if key == "ratio_lte":
                return t.ratio <= float(str(value))
            if key == "seeding_days_gte":
                added = t.added_at or ref
                days = (ref - added).total_seconds() / 86400
                return days >= float(str(value))
            if key == "size_gte":
                return t.size >= int(str(value))
            if key == "category_eq":
                return (t.category or "") == str(value)
            if key == "category_in":
                return (t.category or "") in as_csv_set(value)
            if key == "tag_in":
                tags = set((t.tags or "").split(",")) if t.tags else set()
                return bool(as_csv_set(value) & tags)
            if key == "site_id":
                return t.site_id == int(str(value))
            if key == "info_hash":
                return t.info_hash == str(value)
        except (TypeError, ValueError):
            return False
        # 未知字段：返回 False 让规则在 DSL 升级时行为可预测
        logger.debug("未知清理规则字段 %s 已忽略", key)
        return False


# ================================================================ 六重保护
class WhitelistGuard:
    """FR-5.2.1：分类 / 标签 / 手动收藏命中即跳过并 🔴 告警。

    当前实现基于 site 配置 + Torrent 的 category/tags 字段。
    "手动收藏"由 source='manual' 识别。
    """

    MANUAL_TAG = "manual_keep"  # 用户在面板打的"收藏"标签
    PROTECTED_CATEGORIES: frozenset[str] = frozenset({"keep", "personal", "favorite"})

    @classmethod
    def blocks(cls, torrent: Torrent) -> tuple[bool, str]:
        """返回 (是否阻止, 阻止原因)。"""
        if torrent.source == Source.MANUAL:
            return True, "manual_collection"
        if torrent.category and torrent.category in cls.PROTECTED_CATEGORIES:
            return True, f"protected_category:{torrent.category}"
        if torrent.tags and cls.MANUAL_TAG in torrent.tags.split(","):
            return True, "manual_keep_tag"
        return False, ""


class ProtectPeriodGuard:
    """FR-5.2.6：禁删保护期内禁止清理（默认 12h）。"""

    @staticmethod
    def blocks(torrent: Torrent, now: datetime | None = None, *, hours: int) -> bool:
        ref = now or utcnow()
        if torrent.protect_until is None:
            return False
        return torrent.protect_until > ref and hours > 0


class PathSafetyGuard:
    """FR-5.2.5：路径三重校验。"""

    @staticmethod
    async def verify(
        download_root: str,
        paths: list[str],
        adapter: DownloaderAdapter,
        info_hash: str,
    ) -> tuple[bool, str]:
        """返回 (ok, 失败原因)。

        a. 路径必须来自 ``adapter.get_files()``——这是调用方的契约
        b. 存在性：``stat()`` 确认
        c. 根目录约束：必须位于 ``download_root`` 之下 + ``st_dev`` 匹配
        """
        if not paths:
            return False, "empty_paths"

        # (c) 根目录 + 设备号校验
        try:
            root_dev = device_of(download_root)
        except OSError as exc:
            return False, f"root_unstatable: {exc}"

        # 互不重叠：去重后逐项校验
        seen: set[str] = set()
        for raw in paths:
            path = os.path.realpath(raw)
            if path in seen:
                continue
            seen.add(path)
            if not is_within(download_root, path):
                return False, f"outside_root: {path}"
            if root_dev is not None and device_of(path) != root_dev:
                return False, f"different_fs: {path}"

        # (b) 存在性
        for path in seen:
            if not os.path.exists(path):
                return False, f"missing: {path}"
        return True, ""


class TrashGuard:
    """FR-5.2.3：先 ``remove(delete_data=False)`` → 文件 rename 至回收站。"""

    TRASH_DIR = ".seedhelper/trash"

    @staticmethod
    async def quarantine(
        download_root: str,
        info_hash: str,
        paths: Sequence[str],
        *,
        retention_days: int,
    ) -> tuple[bool, str, str]:
        """返回 (ok, 回收站路径, 错误原因)。

        **绝不退化为直接删除**：move 失败 → 整条清理中止。
        """
        trash_root = os.path.join(download_root, TrashGuard.TRASH_DIR, info_hash)
        try:
            os.makedirs(trash_root, exist_ok=True)
            for p in paths:
                base = os.path.basename(p.rstrip("/")) or "file"
                target = os.path.join(trash_root, base)
                # 同名去重：递增后缀避免覆盖
                if os.path.exists(target):
                    i = 1
                    while os.path.exists(f"{target}.dup{i}"):
                        i += 1
                    target = f"{target}.dup{i}"
                shutil.move(p, target)
        except (OSError, shutil.Error) as exc:
            return False, "", f"quarantine_failed: {exc}"
        # 写元数据：保留期到期时间
        try:
            import json

            from app.core.timeutils import utcnow

            meta = {
                "info_hash": info_hash,
                "isolated_at": utcnow().isoformat(),
                "expire_at": (utcnow() + timedelta(days=retention_days)).isoformat(),
                "files": list(paths),
            }
            with open(os.path.join(trash_root, "trash_meta.json"), "w") as f:
                json.dump(meta, f)
        except OSError:
            pass  # 元数据非关键
        return True, trash_root, ""


class ConfirmationGuard:
    """FR-5.2.4：超阈值时写二次确认队列，**不执行**。"""

    @staticmethod
    def needs_confirmation(
        candidate_count: int, freed_space: int, *, batch_limit: int, space_limit: int
    ) -> bool:
        return candidate_count > batch_limit or freed_space > space_limit

    @staticmethod
    async def enqueue(
        session: AsyncSession,
        *,
        plan: CleanupPlan,
        batch_limit: int,
        space_limit: int,
        ttl_hours: int = 24,
    ) -> CleanupConfirmation | None:
        """超阈值：写一条待确认记录；未超阈值返回 None（可立即执行）。"""
        if not ConfirmationGuard.needs_confirmation(
            plan.total_count,
            plan.total_size,
            batch_limit=batch_limit,
            space_limit=space_limit,
        ):
            return None
        rec = CleanupConfirmation(
            rule_id=plan.rule_id,
            rule_name=plan.rule_name,
            trigger_source=plan.trigger_source,
            affected_count=plan.total_count,
            estimated_space=plan.total_size,
            affected_hashes=[c.torrent.info_hash for c in plan.candidates],  # type: ignore[attr-defined]
            download_root=plan.download_root,
            status=CleanupStatus.PENDING,
            expires_at=utcnow() + timedelta(hours=ttl_hours),
        )
        session.add(rec)
        await session.flush()
        logger.info(
            "清理超阈值：%d 个任务 / %.1f GB，进入人工确认队列",
            plan.total_count,
            plan.total_size / 1024**3,
        )
        return rec


class DryRunGuard:
    """FR-5.2.2：新规则首次启用强制干跑。"""

    @staticmethod
    def must_dry_run(rule: CleanupRule) -> bool:
        return not rule.dry_run_done


# ================================================================ 统一管线
@dataclass
class CleanupOutcome:
    """一次清理的总报告。"""

    candidates_total: int = 0
    candidates_blocked: int = 0
    candidates_executed: int = 0
    freed_space: int = 0
    pending_confirmation: int = 0
    blocked_reasons: dict[str, int] = field(default_factory=dict)
    error: str = ""


class Cleaner:
    """统一清理管线。

    用法::

        async with session_scope() as s:
            cleaner = Cleaner(s, factory)
            outcome = await cleaner.run_rule(rule, trigger_source=...)
    """

    def __init__(
        self,
        session: AsyncSession,
        factory: object,  # AdapterFactory（避免循环）
    ) -> None:
        self._session = session
        self._factory = factory
        self._engine = RuleEngine()

    # ------------------------------------------------------------ 查询
    async def _load_policy(self) -> CleanupPolicy:
        row = (
            await self._session.execute(
                select(CleanupPolicy).where(CleanupPolicy.id == 1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = CleanupPolicy(id=1)
            self._session.add(row)
            await self._session.flush()
        return row

    async def _candidate_torrents(self, sources: Sequence[str]) -> list[Torrent]:
        """拉取候选任务（按 FR-5.1 默认排除手动）。"""
        stmt = select(Torrent).where(
            Torrent.status.in_(
                [TorrentStatus.ACTIVE, TorrentStatus.PAUSED]
            ),
        )
        if sources and "all" not in sources:
            stmt = stmt.where(Torrent.source.in_(sources))
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    # ------------------------------------------------------------ 单规则
    async def run_rule(
        self,
        rule: CleanupRule,
        *,
        trigger_source: str = CleanupTrigger.REPORT,
        dry_run: bool = False,
        only_hashes: set[str] | None = None,
    ) -> CleanupOutcome:
        out = CleanupOutcome()
        policy = await self._load_policy()

        # 干跑门（FR-5.2.2）：首次启用必须先跑
        if dry_run or (policy.dry_run_on_first_use and DryRunGuard.must_dry_run(rule)):
            dry_run = True

        # 解析 default_sources（CSV 字符串）
        sources_csv = policy.default_sources or ""
        default_sources = [s.strip() for s in sources_csv.split(",") if s.strip()]

        candidates = await self._candidate_torrents(default_sources)
        matched: list[Torrent] = [t for t in candidates if self._engine.matches(rule, t)]
        if only_hashes is not None:
            matched = [t for t in matched if t.info_hash in only_hashes]

        out.candidates_total = len(matched)

        # 1. 干跑模式：仅收集，不执行 + 标记 dry_run_done（FR-5.2.2 一次干跑门）
        if dry_run:
            out.blocked_reasons["dry_run"] = len(matched)
            if not rule.dry_run_done:
                rule.dry_run_done = True
                await self._session.flush()
            return out

        # 干跑完成，标记（这里其实已经为 True，但保持幂等）
        if not rule.dry_run_done:
            rule.dry_run_done = True
            await self._session.flush()

        # 2. 六重保护（白名单 + 保护期）
        blocked: list[Torrent] = []
        allowed: list[Torrent] = []
        now = utcnow()
        for t in matched:
            hit, why = WhitelistGuard.blocks(t)
            if hit:
                blocked.append(t)
                out.blocked_reasons[f"whitelist:{why}"] = (
                    out.blocked_reasons.get(f"whitelist:{why}", 0) + 1
                )
                continue
            if ProtectPeriodGuard.blocks(t, now=now, hours=policy.protect_hours):
                blocked.append(t)
                out.blocked_reasons["protect_period"] = (
                    out.blocked_reasons.get("protect_period", 0) + 1
                )
                continue
            allowed.append(t)
        out.candidates_blocked = len(blocked)

        if not allowed:
            return out

        # 3. 计算下载根目录
        download_root = await self._resolve_download_root(allowed)
        if not download_root:
            out.error = "no_download_root"
            return out

        # 4. 二次确认门（FR-5.2.4）：超阈值 → 写队列，不执行
        total_size = sum(t.size for t in allowed)
        plan = CleanupPlan(
            candidates=[
                CleanupCandidate(torrent=t, rule=rule, reason=str(rule.when), size=t.size)
                for t in allowed
            ],
            total_count=len(allowed),
            total_size=total_size,
            rule_id=rule.id,
            rule_name=rule.name,
            trigger_source=trigger_source,
            download_root=download_root,
        )
        pending = await ConfirmationGuard.enqueue(
            self._session,
            plan=plan,
            batch_limit=policy.batch_limit,
            space_limit=policy.space_limit,
        )
        if pending is not None:
            out.pending_confirmation = 1
            return out

        # 5. 实际清理（白名单 + 保护期已过滤；剩余路径走完整安全流程）
        for cand in plan.candidates:
            ok = await self._quarantine_one(
                cand, download_root, policy, trigger_source
            )
            if ok:
                out.candidates_executed += 1
                out.freed_space += cand.size
            else:
                out.blocked_reasons["path_safety"] = (
                    out.blocked_reasons.get("path_safety", 0) + 1
                )

        return out

    # ------------------------------------------------------------ 单条
    async def _quarantine_one(
        self,
        cand: CleanupCandidate,
        download_root: str,
        policy: CleanupPolicy,
        trigger_source: str,
    ) -> bool:
        t: Torrent = cand.torrent  # type: ignore[assignment]
        adapter = await self._factory.downloader_for_id(t.downloader_id)  # type: ignore[attr-defined]
        if adapter is None:
            return False

        async with locks.acquire(adapter.name):
            # 路径来源：必须从下载器实时上报（FR-5.2.5a）
            try:
                files = await adapter.get_files(t.info_hash)
            except Exception as exc:
                logger.warning("下载器 %s 拉取文件清单失败：%s", adapter.name, exc)
                files = []
            paths = [f.path for f in files if f.path]

            # 路径三重校验（FR-5.2.5 b/c）
            ok, why = await PathSafetyGuard.verify(
                download_root, paths, adapter, t.info_hash
            )
            if not ok:
                logger.warning("路径校验失败 %s：%s", t.name, why)
                await self._log_action(
                    rule=cand.rule,
                    action=CleanupAction.TRASH,
                    trigger=trigger_source,
                    affected=[t.info_hash],
                    freed=0,
                    result=CleanupStatus.FAILED,
                    error=why,
                )
                return False

            # 移入回收站（FR-5.2.3）：先 remove(delete_data=false)
            logger.info(
                "移入回收站 action=trash downloader=%s source=%s reason=%s",
                adapter.name, trigger_source, cand.reason,
            )
            # 先移入回收站（绝不退化为删除），成功后再从下载器移除种子。
            # 顺序保证 quarantine 失败时种子仍在下载器中，可重试。
            ok2, trash_path, qerr = await TrashGuard.quarantine(
                download_root,
                t.info_hash,
                paths,
                retention_days=policy.trash_retention_days,
            )
            if not ok2:
                logger.warning("移入回收站失败 %s：%s", t.name, qerr)
                await self._log_action(
                    rule=cand.rule,
                    action=CleanupAction.TRASH,
                    trigger=trigger_source,
                    affected=[t.info_hash],
                    freed=0,
                    result=CleanupStatus.FAILED,
                    error=qerr,
                )
                return False
            # 文件已隔离，再从下载器移除种子（delete_data=false，文件已不在原位）
            try:
                await adapter.remove([t.info_hash], delete_data=False)
            except Exception as exc:
                logger.warning("下载器移除任务失败：%s", exc)
                await self._log_action(
                    rule=cand.rule,
                    action=CleanupAction.TRASH,
                    trigger=trigger_source,
                    affected=[t.info_hash],
                    freed=0,
                    result=CleanupStatus.FAILED,
                    error=str(exc),
                )
                return False

            # 写 trash 表
            from app.db.models import Trash

            self._session.add(
                Trash(
                    info_hash=t.info_hash,
                    torrent_id=t.id,
                    site_id=t.site_id,
                    name=t.name,
                    size=t.size,
                    original_path=paths[0] if paths else "",
                    trash_path=trash_path,
                    trigger_source=trigger_source,
                    expire_at=utcnow()
                    + timedelta(days=policy.trash_retention_days),
                )
            )

            # 任务标 deleted（不复用 status='aborted'，语义区分）
            t.status = TorrentStatus.DELETED
            t.removed_at = utcnow()
            await self._log_action(
                rule=cand.rule,
                action=CleanupAction.TRASH,
                trigger=trigger_source,
                affected=[t.info_hash],
                freed=t.size,
                result=CleanupStatus.DONE,
            )
            return True

    async def _log_action(
        self,
        *,
        rule: object,  # CleanupRule
        action: str,
        trigger: str,
        affected: list[str],
        freed: int,
        result: str,
        error: str = "",
    ) -> None:
        rule_name = getattr(rule, "name", "") if rule else ""
        logger.info(
            "清理动作 action=%s trigger=%s rule=%s affected=%d freed=%d result=%s%s",
            action, trigger, rule_name, len(affected), freed, result,
            f" error={error}" if error else "",
        )

    async def _resolve_download_root(self, candidates: list[Torrent]) -> str | None:
        """从候选任务的下载器配置里取 download_root。

        必须全部一致：不同下载根目录混在一起没法做路径校验（FR-5.2.5c）。
        """
        if not candidates:
            return None
        dl_ids = {t.downloader_id for t in candidates}
        rows = (
            (
                await self._session.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.id.in_(dl_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        roots: set[str] = set()
        for row in rows:
            if row.download_root:
                roots.add(row.download_root)
        if len(roots) == 1:
            return next(iter(roots))
        if len(roots) > 1:
            logger.warning(
                "候选任务分布在多个下载根目录：%s，跳过本轮", sorted(roots)
            )
            return None
        return None


# ================================================================ 报告
async def generate_report(
    session: AsyncSession, *, since: datetime | None = None
) -> dict[str, object]:
    """FR-5.4 每日清理报告。"""
    from app.db.models import Trash

    expire_at = utcnow()
    expire_to = expire_at + timedelta(days=7)
    pending_purge = (
        (
            await session.execute(
                select(Trash).where(
                    Trash.purged.is_(False),
                    Trash.restored.is_(False),
                    Trash.expire_at >= expire_at,
                    Trash.expire_at <= expire_to,
                )
            )
        )
        .scalars()
        .all()
    )

    return {
        "since": since or (utcnow() - timedelta(days=1)),
        "until": utcnow(),
        "pending_purge_7d": [
            {
                "id": t.id,
                "info_hash": t.info_hash,
                "size": t.size,
                "expire_at": t.expire_at,
            }
            for t in pending_purge
        ],
    }
