"""ORM 模型（§8 关键数据模型）。

M1 建立全部表结构（M2~M5 只填充逻辑）。时间列统一 naive UTC。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    """naive UTC，替代已废弃的 ``_utcnow()``（Python 3.12）。"""
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------- 枚举常量
class Source:
    MANUAL = "manual"
    AUTO_SEED = "auto_seed"
    FREE_SEED = "free_seed"


class TorrentStatus:
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FREE_COMPLETED = "free_completed"
    FREE_COMPLETED_LATE = "free_completed_late"
    FREE_MISSED = "free_missed"
    ABORTED = "aborted"
    DELETED = "deleted"


class FreeType:
    TIMED = "freeleech_timed"
    PERMANENT = "freeleech_permanent"
    TWOU_TIMED = "twoup_timed"
    NEUTRAL = "neutral_leech"
    UNKNOWN = "unknown"


class FreeSource:
    API = "api"
    LIST_TEXT = "list_text"
    RSS_DESC = "rss_desc"
    SITE_DEFAULT = "site_default"
    LEARNED = "learned"


class AbortedReason:
    WINDOW_EXPIRED = "window_expired"
    ERROR = "error"
    PAUSED = "paused"
    MANUAL = "manual"
    DISK_FULL = "disk_full"
    TRACKER_FAIL = "tracker_fail"


class CooldownReason:
    USER_REMOVED = "user_removed"
    USER_BLACKLISTED = "user_blacklisted"
    WINDOW_EXPIRED = "window_expired"
    DEAD_TORRENT = "dead_torrent"
    DISK_FULL = "disk_full"
    DUPLICATE = "duplicate"
    MANUAL_SKIP = "manual_skip"


class PausedReason:
    YIELD = "yield"
    USER = "user"
    SYSTEM = "system"


class TriggerSource:
    SCHEDULE = "schedule"
    MANUAL = "manual"
    WINDOW_ABORT = "window_abort"
    RULE = "rule"


class SkipReason:
    """FR-2.3 七项过滤的跳过原因（逐条可追溯）。"""

    DUPLICATE_LOCAL = "duplicate_local"
    DUPLICATE_SITE = "duplicate_site"
    CATEGORY_MISMATCH = "category_mismatch"
    SIZE_EXCEEDED = "size_exceeded"
    COOLDOWN = "cooldown"
    DISK_FULL = "disk_full"
    PATH_INVALID = "path_invalid"
    NO_LOCAL_MATCH = "no_local_match"


class Decision:
    PENDING = "pending"
    ACCEPTED = "accepted"
    SKIPPED = "skipped"
    FAILED = "failed"


class CleanupTrigger:
    """清理触发源（FR-5.1）：用于规则匹配与统计。"""

    AUTO_SEED = "auto_seed"
    FREE_SEED = "free_seed"
    MANUAL = "manual"
    #: FR-4.1 异常自动处置（M3 已枚举）
    CLIENT_ERROR = "client_error"
    TORRENT_DELETED = "torrent_deleted"
    TORRENT_ERROR = "torrent_error"
    #: FR-5.3 空间压力触发的清理
    SPACE_PRESSURE = "space_pressure"
    #: FR-5.4 报告/汇总
    REPORT = "report"


class CleanupStatus:
    """FR-5.1 / FR-5.2.4：清理任务状态机。"""

    PENDING = "pending"            # 干跑完成，等待用户确认
    CONFIRMED = "confirmed"        # 用户已二次确认，待执行
    RUNNING = "running"            # 正在执行
    DONE = "done"                  # 完成
    FAILED = "failed"              # 执行失败
    CANCELLED = "cancelled"        # 用户取消
    SKIPPED = "skipped"            # 保护机制触发，跳过


class CleanupAction:
    """FR-5.2 全部动作类型。"""

    DRY_RUN = "dry_run"
    CONFIRM = "confirm"
    TRASH = "trash"
    PURGE = "purge"
    RESTORE = "restore"


#: 永久免费不参与超时中止（FR-3.0 / R10）
NON_ABORTABLE_FREE_TYPES: Final = frozenset({FreeType.PERMANENT, FreeType.NEUTRAL})


# ---------------------------------------------------------------- 基础表
class DownloaderInstance(Base):
    """下载器实例。"""

    __tablename__ = "downloader_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_root: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="路径根目录校验基准；无此值则禁用数据删除",
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class Site(Base):
    """站点配置。"""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    domain: Mapped[str] = mapped_column(String(255))
    adapter: Mapped[str] = mapped_column(String(64), default="mteam")

    # 认证与凭据（加密存储）
    rss_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    apikey_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    cookies_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    min_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class Torrent(Base):
    """种子池（§8 torrents）。"""

    __tablename__ = "torrents"
    __table_args__ = (
        Index("ix_torrents_status_abort_at", "status", "abort_at"),
        Index("ix_torrents_source_status_free_end", "source", "status", "free_end_at"),
        Index("ix_torrents_site_source", "site_id", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    info_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(1024))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    downloader_id: Mapped[int] = mapped_column(ForeignKey("downloader_instances.id"))

    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[str | None] = mapped_column(String(512), nullable=True)
    save_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    ratio: Mapped[float] = mapped_column(Float, default=0.0)
    uploaded: Mapped[int] = mapped_column(BigInteger, default=0)
    downloaded: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    num_seeds: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[str] = mapped_column(String(16), default=Source.MANUAL)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default=TorrentStatus.ACTIVE)

    # 免费窗口锚点（FR-3.4）
    free_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    free_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    free_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    free_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    free_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    clock_offset: Mapped[int] = mapped_column(Integer, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    abort_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    window_slack: Mapped[int | None] = mapped_column(Integer, nullable=True)

    paused_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    aborted_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    aborted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trash_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_rescued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
        comment="FR-3.8 抢救策略最后执行时间，用于限速后冷却"
    )
    protect_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: FR-2.5 空做种连续观察轮数（持久化跨轮计数）
    zero_seed_rounds: Mapped[int] = mapped_column(Integer, default=0)


class CooldownHash(Base):
    """冷却黑名单（FR-2.3.1，按 reason 阶梯）。"""

    __tablename__ = "cooldown_hash"
    __table_args__ = (
        UniqueConstraint("site_id", "info_hash", name="uq_cooldown_site_hash"),
        Index("ix_cooldown_lookup", "site_id", "info_hash", "cooldown_until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    info_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(32))
    fail_count: Mapped[int] = mapped_column(Integer, default=1)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AddHistory(Base):
    """辅种添加历史（FR-2.3「站点去重」）。

    **与 :class:`CooldownHash` 的职责边界**（FR-2.3.1）：

    - 本表 = **幂等凭证**。"本站在此站添加过这个 hash" 是不可协商的事实，
      永久保留，是非功能·幂等性的落地。
    - ``cooldown_hash`` = **临时回避**。到期即失效，某些 ``reason``
      （``user_removed`` / ``user_blacklisted``）甚至需要人工解除。

    二者不得互相替代：``disk_full`` / ``duplicate`` 记入 history 但**不**冷却，
    空间恢复或规则变化后必须能重新辅种。
    """

    __tablename__ = "add_history"
    __table_args__ = (
        UniqueConstraint("site_id", "info_hash", name="uq_history_site_hash"),
        Index("ix_history_torrent", "torrent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    info_hash: Mapped[str] = mapped_column(String(64))
    torrent_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="可空：下载器添加失败时仅有记录"
    )
    name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=Source.AUTO_SEED)
    outcome: Mapped[str] = mapped_column(String(24), default="added")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class FeedItemRecord(Base):
    """RSS 拉取到的原始条目（FR-2.2「拉取结果写入本地 SQLite」）。

    与 :class:`AddHistory` 的区别：本表记录的是**看过但未添加**的条目，
    用于排查"为什么这个种没被辅种"（FR-2.3 每一项过滤都要可追溯）。
    """

    __tablename__ = "feed_items"
    __table_args__ = (
        Index("ix_feed_items_feed_seen", "feed_id", "seen_at"),
        Index("ix_feed_items_hash", "info_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"))
    info_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(1024))
    link: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 过滤结果：``accepted`` 或具体 skip 原因（FR-2.3 七项）
    decision: Mapped[str] = mapped_column(String(32), default="pending")
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Trash(Base):
    """回收站（FR-5.2.3 / FR-3.5 中止数据隔离）。"""

    __tablename__ = "trash"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    info_hash: Mapped[str] = mapped_column(String(64), index=True)
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    site_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    original_path: Mapped[str] = mapped_column(String(1024))
    trash_path: Mapped[str] = mapped_column(String(1024))
    trigger_source: Mapped[str] = mapped_column(String(32), default=TriggerSource.RULE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expire_at: Mapped[datetime] = mapped_column(DateTime)
    restored: Mapped[bool] = mapped_column(Boolean, default=False)
    purged: Mapped[bool] = mapped_column(Boolean, default=False)


class CleanupPolicy(Base):
    """清理策略（单行，FR-5.2 全局配置）。"""

    __tablename__ = "cleanup_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: FR-5.2.2 新规则首次启用强制干跑
    dry_run_on_first_use: Mapped[bool] = mapped_column(Boolean, default=True)
    #: FR-5.2.4 二次确认阈值（任务数）
    batch_limit: Mapped[int] = mapped_column(Integer, default=20)
    #: FR-5.2.4 二次确认阈值（释放空间 / 字节）
    space_limit: Mapped[int] = mapped_column(BigInteger, default=200 * 1024**3)
    #: FR-5.2.6 禁删保护期（小时）
    protect_hours: Mapped[int] = mapped_column(Integer, default=12)
    #: FR-5.2.3 回收站保留期（天）
    trash_retention_days: Mapped[int] = mapped_column(Integer, default=7)
    #: FR-5.3 磁盘低水位（字节）
    low_water: Mapped[int] = mapped_column(BigInteger, default=50 * 1024**3)
    #: FR-5.3 回升目标（字节）
    high_water: Mapped[int] = mapped_column(BigInteger, default=100 * 1024**3)
    #: 默认清理源（FR-5.1 维度 1）：默认不含手动任务
    default_sources: Mapped[str] = mapped_column(String(64), default="auto_seed,free_seed")


class CleanupRule(Base):
    """FR-5.1 清理规则。

    ``when`` 存 JSON DSL，按字段嵌套匹配；具体 DSL 见
    :class:`~app.services.cleanup.RuleEngine`。新增字段需向后兼容——
    引擎对未知字段应跳过而非报错。
    """

    __tablename__ = "cleanup_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    #: DSL 条件表达式，如 {"ratio_gte": 3.0, "seeding_days_gte": 30}
    when: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    #: 已完成干跑（FR-5.2.2）
    dry_run_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class CleanupConfirmation(Base):
    """FR-5.2.4 二次确认队列。

    单次清理涉及任务数 / 空间超阈值时，**不执行**，写一条记录供人工确认。
    用户在面板点击"确认"才真正执行。
    """

    __tablename__ = "cleanup_confirmations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("cleanup_rules.id", ondelete="SET NULL"), nullable=True
    )
    rule_name: Mapped[str] = mapped_column(String(128))
    trigger_source: Mapped[str] = mapped_column(String(32))
    affected_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_space: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 待删除任务的 infohash 列表（JSON）—— 确认时按此执行，避免重算
    affected_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: FR-5.2.5c 下载根目录：执行时校验
    download_root: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), default=CleanupStatus.PENDING
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CleanupLog(Base):
    """清理动作审计日志（FR-5.4 报告 / 排障）。

    与 :class:`Trash` 的区别：本表记录**所有**清理决策的结果（含白名单
    拦截、保护期、二次确认路径失败的），便于追溯"为什么没删 / 为什么删了"。
    """

    __tablename__ = "cleanup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("cleanup_rules.id", ondelete="SET NULL"), nullable=True
    )
    rule_name: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))  # CleanupAction.*
    trigger_source: Mapped[str] = mapped_column(String(32))
    affected: Mapped[list[str]] = mapped_column(JSON, default=list)
    freed_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    result: Mapped[str] = mapped_column(String(16))  # CleanupStatus.*
    error: Mapped[str] = mapped_column(String(512), default="")


class Feed(Base):
    """RSS 源（FR-2.2）。"""

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    kind: Mapped[str] = mapped_column(String(32), default="site_rss")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_item_count: Mapped[int] = mapped_column(Integer, default=0)
    last_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    breaker_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class FreeCandidate(Base):
    """免费种候选池（用户手动选择推送）。"""

    __tablename__ = "free_candidates"
    __table_args__ = (
        UniqueConstraint("site_id", "item_id", name="uq_candidate_site_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    #: 站内 ID（m-team 数字 ID / NexusPHP details.php?id=X）。允许 None（id 缺失跳过）
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: m-team search 不返回 hash，download_torrent 后回填
    info_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    free_type: Mapped[str] = mapped_column(String(32), default=FreeType.UNKNOWN)
    free_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    free_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    free_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 原始免费标记（m-team status dict / NexusPHP free_classes + remaining_*）
    free_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    #: 已签名 download URL（NexusPHP 含 passkey/sign；mteam 走 genDlToken 不需要）
    #: 入库时由 adapter 在 pull 阶段填好，推送时直接 GET 即可，绕过裸 /download.php?id=X 的 404。
    torrent_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: pending / pushed / skipped / failed
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 推送成功后回填（torrents.id）
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SiteWindowStat(Base):
    """各站窗口时长历史分布，供自学习估算（FR-3.0 来源 5）。"""

    __tablename__ = "site_window_stats"
    __table_args__ = (
        UniqueConstraint("site_id", "free_type", name="uq_site_stat_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    free_type: Mapped[str] = mapped_column(String(32))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_p50: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_p75: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    avg_slack: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class Rule(Base):
    """清理/告警规则（FR-5.1）。"""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32), default="cleanup")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run_done: Mapped[bool] = mapped_column(Boolean, default=False)
    when: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class TaskQueue(Base):
    """待执行命令队列（崩溃恢复 / 幂等重放，非功能·可靠性/幂等性）。"""

    __tablename__ = "task_queue"
    __table_args__ = (
        UniqueConstraint("idem_key", name="uq_task_queue_idem"),
        Index("ix_task_queue_status", "status", "run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idem_key: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )