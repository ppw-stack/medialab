"""ORM 模型（§8 关键数据模型）。

M1 建立全部表结构（M2~M5 只填充逻辑）。时间列统一 naive UTC。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar, Final

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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class RescueStrategy:
    NOTIFY_ONLY = "notify_only"
    SPEED_UP = "speed_up"
    YIELD_THEN_RETRY = "yield_then_retry"
    ABORT_NOW = "abort_now"


class TriggerSource:
    SCHEDULE = "schedule"
    MANUAL = "manual"
    ALERT_LINKED = "alert_linked"
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


class AlertLevel:
    """告警级别（FR-4.3）。

    顺序有意义：:func:`rank` 用于渠道 ``min_level`` 比较。
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    ORDER: ClassVar[dict[str, int]] = {INFO: 0, WARNING: 1, CRITICAL: 2}

    @staticmethod
    def rank(level: str) -> int:
        """未知级别按最严重处理——宁可多提醒，不可漏掉严重问题。"""
        return AlertLevel.ORDER.get(level, 99)


class AlertCategory:
    """FR-4.1 十二项健康指标分类。"""

    TORRENT_DELETED = "torrent_deleted"
    TORRENT_ERROR = "torrent_error"
    ILLEGAL_TORRENT = "illegal_torrent"
    ZERO_SEEDS = "zero_seeds"
    RATIO_LOW = "ratio_low"
    SEED_TIME_SHORT = "seed_time_short"
    ZERO_DOWNLOAD_SPEED = "zero_download_speed"
    CONN_LIMIT = "conn_limit"
    RATE_LIMIT_CHANGED = "rate_limit_changed"
    PORT_UNREACHABLE = "port_unreachable"
    DISK_LOW = "disk_low"
    CLIENT_ERROR = "client_error"


#: 永久免费不参与超时中止（FR-3.0 / R10）
NON_ABORTABLE_FREE_TYPES: Final = frozenset({FreeType.PERMANENT, FreeType.NEUTRAL})


# ---------------------------------------------------------------- 基础表
class DownloaderInstance(Base):
    """下载器实例（FR-1.1：≥3 个实例，独立配置与开关）。"""

    __tablename__ = "downloader_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 反向关系：指向 sites.downloader_id（M2 用）
    sites: Mapped[list[Site]] = relationship(back_populates="downloader")
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32))  # qbittorrent/transmission/deluge/aria2
    url: Mapped[str] = mapped_column(String(512))
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    download_root: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="FR-5.2.5c 路径根目录校验基准；无此值则禁用数据删除",
    )
    max_upload: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_download: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 是否校验 TLS 证书（自签名 qBittorrent 可关闭，FR-1 安全建议保持 True）
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 能力声明快照（Set[Feature] 的序列化），含 get_files 是否可用（R12）
    capabilities: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )


class Site(Base):
    """站点配置（FR-2.1 + FR-1.5.2 站点差异配置化）。"""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    domain: Mapped[str] = mapped_column(String(255))
    adapter: Mapped[str] = mapped_column(String(64), default="generic")

    # 认证与凭据（加密存储）
    rss_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    webui_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)

    # 辅种策略
    downloader_id: Mapped[int | None] = mapped_column(
        ForeignKey("downloader_instances.id"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(64), default="cross-seed")
    save_path_template: Mapped[str] = mapped_column(
        String(512), default="/data/torrents/{site}/{name}"
    )
    max_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    min_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # 免费窗口（FR-3.0 / FR-3.0.1 / FR-3.0.2）
    default_free_duration: Mapped[int] = mapped_column(Integer, default=72 * 3600)
    min_buffer: Mapped[int] = mapped_column(Integer, default=1800)
    buffer_ratio: Mapped[float] = mapped_column(Float, default=0.10)
    clock_offset: Mapped[int] = mapped_column(Integer, default=0)
    clock_offset_probed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    rescue_strategy: Mapped[str] = mapped_column(
        String(32), default=RescueStrategy.NOTIFY_ONLY
    )
    grab_short_window: Mapped[bool] = mapped_column(Boolean, default=False)
    free_size_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    free_size_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 站点差异配置化（FR-1.5.2）
    detail_path_template: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    rss_time_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_field_map: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    free_type_map: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    download_factor_map: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    time_unit_multiplier: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    downloader: Mapped[DownloaderInstance | None] = relationship(
        back_populates="sites"
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
    """清理策略（单行，FR-5.2 全局配置）。

    与 :class:`~app.db.models.AlertPolicy` 同样模式：唯一 id=1 占位。
    """

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
        ForeignKey("cleanup_rules.id"), nullable=True
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
    """FR-5.2.7 审计 + FR-5.4 报告：每次清理动作一条记录。"""

    __tablename__ = "cleanup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(32))
    trigger_source: Mapped[str] = mapped_column(String(32))
    affected_count: Mapped[int] = mapped_column(Integer, default=0)
    freed_space: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 被影响的任务 hash 列表（JSON）
    hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    #: 命中原因（每条任务的简短说明）
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(16))  # done/failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ActionLog(Base):
    """操作审计（FR-2.6 / FR-5.2.7）。"""

    __tablename__ = "action_logs"
    __table_args__ = (Index("ix_action_logs_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    action: Mapped[str] = mapped_column(String(64))
    trigger_source: Mapped[str] = mapped_column(String(32))
    downloader_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    site_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hash_count: Mapped[int] = mapped_column(Integer, default=0)
    freed_space: Mapped[int] = mapped_column(BigInteger, default=0)
    result: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


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
    """免费种候选与结果（FR-3.1/FR-3.2）。"""

    __tablename__ = "free_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    info_hash: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    free_type: Mapped[str] = mapped_column(String(32), default=FreeType.UNKNOWN)
    free_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    free_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    free_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32), default="pending")
    reject_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


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


class Alert(Base):
    """告警记录（FR-4.2 / FR-4.3）。

    ``dedupe_key`` 是 FR-4.3 去重聚合的依据，形式
    ``category:info_hash``。同一 key 在 ``mute_window`` 内不重复推送。
    """

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_dedupe", "dedupe_key", "ts"),
        Index("ix_alerts_level_ts", "level", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(32))
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    info_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    #: 去重键 ``category:info_hash``（FR-4.3）
    dedupe_key: Mapped[str] = mapped_column(String(160), default="")
    #: 静默截止：在此之前不重复推送
    muted_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 已投递的渠道（JSON 列表），用于"已发过就不再发"
    notified_channels: Mapped[list[str]] = mapped_column(JSON, default=list)


class NotifyChannel(Base):
    """通知渠道配置（FR-4.2，多渠道并行、可单独开关）。

    ``secret_enc`` 存 webhook / bot token / SMTP 密码等敏感项（FR-7）。
    """

    __tablename__ = "notify_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    #: wecom | serverchan | telegram | email | bark | webhook | panel
    type: Mapped[str] = mapped_column(String(24))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 非敏感配置：webhook URL 模板、chat_id、SMTP host 等
    config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    #: 仅接收该级别及以上的告警（info < warning < critical）
    min_level: Mapped[str] = mapped_column(String(8), default="warning")
    #: 连续失败次数，超过则临时熔断该渠道（防止坏 webhook 拖慢巡检）
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    breaker_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AlertPolicy(Base):
    """告警策略（FR-4.3，单行全局配置）。"""

    __tablename__ = "alert_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: 去重窗口（小时），默认 6
    mute_window_hours: Mapped[int] = mapped_column(Integer, default=6)
    #: 🟡 是否每日汇总一次（FR-4.3 分级静默）
    warning_daily_digest: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 🔴 是否绕过汇总立即推送
    critical_immediate: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 风暴保护阈值：单日告警超过该数即降级为汇总推送
    storm_threshold: Mapped[int] = mapped_column(Integer, default=50)
    #: 静默时段，格式 ``HH:MM-HH:MM``（FR-4.3）；🔴 不受此限制
    quiet_hours: Mapped[str | None] = mapped_column(String(16), nullable=True)


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