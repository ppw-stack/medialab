"""ORM 模型（§8 关键数据模型）。

M1 建立全部表结构（M2~M5 只填充逻辑）。时间列统一北京时间字符串。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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


def _bj_now() -> str:
    """北京时间字符串，替代已废弃的 ``_utcnow()``。"""
    from app.core.timeutils import bj_now
    return bj_now()


# ---------------------------------------------------------------- 枚举常量
class FreeType:
    TIMED = "freeleech_timed"
    PERMANENT = "freeleech_permanent"
    TWOU_TIMED = "twoup_timed"
    NEUTRAL = "neutral_leech"
    UNKNOWN = "unknown"
    #: 退还/返还种（springsunday "1d 退还"）：上传者已撤种，窗口内仍可下载
    #: 业务规则：不自动推送（push_free 排除）；monitor 视为"非免费"直接暂停
    REFUND = "refund"


class PushStatus:
    """torrent_index 推送状态（替代原 torrents 表去重）。"""

    PENDING = "pending"
    PUSHED = "pushed"
    FAILED = "failed"


class FreeSource:
    API = "api"
    LIST_TEXT = "list_text"
    SITE_DEFAULT = "site_default"


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
    #: 默认保存路径（推送/添加种子时传给下载器：qB savepath / TR download_dir）。
    #: 下载器容器视角的路径；留空表示用下载器自身默认路径。
    save_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 标记此下载器用于辅种监测。
    #: 调度作业 ``scan_cross_seed``（cron ``0 */6 * * *``）扫描所有
    #: is_cross_seed=True 的实例, 检测 tracker 未注册 / 数据丢失两类异常
    #: 并自动暂停（保留数据文件, 不删除）。
    is_cross_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
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

    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


class TorrentIndex(Base):
    """通用站点拉取索引：所有站点的种子列表（API/HTML）抓取结果。

    单个调度任务 ``fetch_all_sites``（cron ``*/10 * * * *``）按站遍历
    调用各 adapter 的 :meth:`SiteAdapter.search_torrents`，把返回的
    :class:`FeedItem` 全部 upsert 写到这里（唯一约束 ``(site_id, item_id)``）。

    历史背景：替代原 ``free_candidates``（手动推送候选池）+ ``Feed``
    + ``FeedItemRecord``。统一成一张"站点拉取快照"——
    任何后续工作（外部工具、SQL 查询、辅种匹配、手动筛选）都从这里读。
    """

    __tablename__ = "torrent_index"
    __table_args__ = (
        UniqueConstraint("site_id", "item_id", name="uq_torrent_index_site_item"),
        Index("ix_torrent_index_last_seen", "last_seen_at"),
        Index("ix_torrent_index_info_hash", "info_hash"),
        Index("ix_torrent_index_site_free", "site_id", "free_type"),
        Index("ix_torrent_index_push_status", "push_status"),
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
    free_end_at: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    free_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    free_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 具体下载折扣百分比（NEUTRAL 时为 0–100；永久免费固定 100；其他 NULL）
    discount_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 原始 free payload（mteam status dict / NexusPHP free_classes + remaining_*）
    free_payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    published_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 首次入库时间（历史分析用）
    first_seen_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    #: 最近一次拉取时间（清理过期数据参考）
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_bj_now, index=True)
    #: 推送状态（NULL = 未推过；pushed = 已推到默认下载器；failed = 推送失败）
    #: 索引由 __table_args__ 的 Index("ix_torrent_index_push_status") 提供——
    #: 此处不要加 index=True，否则与 __table_args__ 生成同名索引，
    #: create_all 在新库上会报 "index already exists"。
    push_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 推送时间
    pushed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


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
    run_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
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
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


class JobRun(Base):
    """调度作业单次执行历史（cron 触发 + 手动触发均记录）。"""

    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_job_started", "job_name", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    #: 触发方式：'cron' | 'manual'
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[str] = mapped_column(String(32), default=_bj_now, index=True)
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 作业返回的 dict（JSON 序列化后存储）
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class SiteUserData(Base):
    """站点用户数据快照（FR-1.6 站点用户数据契约·精简版）。

    ``refresh_user_data`` 作业每 30 分钟调一次各适配器的
    :meth:`SiteAdapter.get_user_snapshot`，把 4 业务字段
    upload / download / seeding_count / leeching_count
    upsert 到本表（``site_id`` UNIQUE）。

    字段语义详见 :mod:`app.adapters.sites.user_data`。
    """

    __tablename__ = "site_user_data"
    __table_args__ = (
        Index("ix_site_user_data_site_seen", "site_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 关联站点（一站一行）
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id"), unique=True, index=True
    )
    #: 上传量（字节）
    upload: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 下载量（字节）
    download: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 当前做种数
    seeding_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 当前下载/未完成数
    leeching_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 最近一次刷新时间（北京时间）
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )
