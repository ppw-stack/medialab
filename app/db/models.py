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
    #: 能力声明快照（Set[Feature] 的序列化），含 get_files 是否可用（R12）
    capabilities: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow(), onupdate=_utcnow()
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow(), onupdate=_utcnow()
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

    added_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    protect_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    last_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    expire_at: Mapped[datetime] = mapped_column(DateTime)
    restored: Mapped[bool] = mapped_column(Boolean, default=False)
    purged: Mapped[bool] = mapped_column(Boolean, default=False)


class ActionLog(Base):
    """操作审计（FR-2.6 / FR-5.2.7）。"""

    __tablename__ = "action_logs"
    __table_args__ = (Index("ix_action_logs_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
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
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())


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
        DateTime, default=_utcnow(), onupdate=_utcnow()
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())


class Alert(Base):
    """告警记录（FR-4.2 / FR-4.3）。"""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow(), index=True)
    level: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(32))
    torrent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    info_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


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
    run_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow(), onupdate=_utcnow()
    )