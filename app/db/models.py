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
    AUTO_PUSH = "auto_push"


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
    password: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # 认证与凭据
    rss_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    apikey: Mapped[str | None] = mapped_column(Text, nullable=True)
    cookies: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        UniqueConstraint(
            "site_id", "item_id", "downloader_id",
            name="uq_torrents_site_item_dl",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: BT 协议级 SHA-1。qB/Tr add_torrent 不一定返回（已知 qB 返空字符串），
    #: 此时留 NULL；Transmission 返真实 hash。
    info_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(1024))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    downloader_id: Mapped[int] = mapped_column(ForeignKey("downloader_instances.id"))
    #: 站内 ID（m-team 数字 / NexusPHP details.php?id=X），用于跨表去重
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[str | None] = mapped_column(String(512), nullable=True)
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


class TorrentIndex(Base):
    """通用拉取索引：所有站点的 RSS feed 拉取结果。

    单个调度任务 ``fetch_all_sites``（cron ``*/10 * * * *``）按站遍历
    调用各 adapter 的 :meth:`SiteAdapter.search_torrents`，把返回的
    :class:`FeedItem` 全部 upsert 写到这里（唯一约束 ``(site_id, item_id)``）。

    历史背景：替代原 ``free_candidates``（手动推送候选池）+ ``Feed``
    + ``FeedItemRecord``（RSS audit）。统一成一张"RSS 拉取快照"——
    任何后续工作（外部工具、SQL 查询、辅种匹配、手动筛选）都从这里读。
    """

    __tablename__ = "torrent_index"
    __table_args__ = (
        UniqueConstraint("site_id", "item_id", name="uq_torrent_index_site_item"),
        Index("ix_torrent_index_last_seen", "last_seen_at"),
        Index("ix_torrent_index_info_hash", "info_hash"),
        Index("ix_torrent_index_site_free", "site_id", "free_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    #: 站内 ID（m-team 数字 ID / NexusPHP details.php?id=X）
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: m-team 拉取时一般无 hash；NexusPHP 留空等 push 时回填
    info_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    free_type: Mapped[str] = mapped_column(String(32), default=FreeType.UNKNOWN)
    free_end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    free_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    free_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 具体下载折扣百分比（NEUTRAL 时为 0–100；永久免费固定 100；其他 NULL）
    discount_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 原始 free payload（mteam status dict / NexusPHP free_classes + remaining_*）
    free_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 首次入库时间（历史分析用）
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    #: 最近一次拉取时间（清理过期数据参考）
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


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


class AppRuntimeParam(Base):
    """应用运行时参数（DB 持久化的可调设置，启动期从 env 加载，运行期可改）。

    单行 kv 表，键名 = 参数名（与前端表单字段一致）：
    - ``push_min_remaining_hours``：限时免费种最低剩余小时数
    - ``push_max_per_run``：每次推送免费种上限
    - ``monitor_pre_pause_minutes``：免费窗口监测提前暂停分钟数

    值类型与 :class:`AppSettings` 一致：int / str / float 任一。
    """

    __tablename__ = "app_runtime_params"

    #: 参数名（PK）—— 与前端字段同名
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 整型值（与前端 input-number 一致；str 字段留作未来扩展）
    value_int: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_str: Mapped[str | None] = mapped_column(String(256), nullable=True)
    #: 最近一次修改时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )