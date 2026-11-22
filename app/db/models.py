"""ORM 模型（§8 关键数据模型）。

**两个业务域共处一库**：

- **辅种域**（ptHelper 基线）—— 站点、下载器、种子索引、调度。
- **媒体库域**（strmmanger 并入）—— 存储源、STRM 条目、直链缓存、
  WebDAV 账号与审计、上传任务。表名与辅种域**零冲突**，外键各自闭环
  （辅种域 FK 指向 ``sites`` / ``downloader_instances``，媒体库域 FK 指向
  ``drivers``）。

**DB 时间列已统一为北京时间字符串**（``String(32)``，:func:`_bj_now`）：

- 两域所有「DB 记录时刻」类列（``created_at`` / ``updated_at`` /
  ``last_*_at`` / ``probed_at`` / ``mtime`` 等）一律北京时间字符串。
- **协议侧时刻**保留 unix 秒（``Integer``）——它们与 ``time.time()``
  直接比较，语义是「绝对时刻/时间差」而非「DB 记录时刻」：
    - ``link_cache.expires_at`` —— L2 缓存过期时刻
    - ``cross_seed_snapshot.stats[].added_at`` —— 下载器入队时刻（JSON 内）
    - 进程内 token 有效期（非 DB 列）

统一判据是**语义**，不是「属于哪个域」：辅种域的 ``added_at`` 是
unix 秒（协议侧），媒体库域的 ``created_at`` 是字符串（DB 记录时刻）。

注：领域枚举（``FreeType`` / ``PushStatus`` / ``FreeSource``）住在
:mod:`app.domain.enums`——它们是业务概念，不属于数据库层。本模块
**不再 re-export**：需要枚举请直接
``from app.domain.enums import FreeType``（避免"枚举住在 db 层"的
错觉重新长回来）。
"""

from __future__ import annotations

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

from app.domain.enums import FreeType
from app.domain.media_exts import (
    DEFAULT_DATA_EXTS,
    DEFAULT_IMAGE_EXTS,
    DEFAULT_VIDEO_EXTS,
)
from app.domain.upload_types import DEFAULT_UPLOAD_CONCURRENCY

__all__ = [
    "AggregateGroup",
    "AggregateGroupRoot",
    "AppEnvSetting",
    "Base",
    "CrossSeedOrphan",
    "CrossSeedSnapshot",
    "DavAuditLog",
    "DavUser",
    "DownloaderInstance",
    "Driver",
    "JobRun",
    "LinkCacheEntry",
    "MediaFile",
    "MediaScrape",
    "OrganizeHistory",
    "ProbeResult",
    "SchedulerJobSetting",
    "SchedulerJobSettingBackup",
    "ScrapeAlias",
    "ScrapeCache",
    "SecondaryStaging",
    "Setting",
    "Site",
    "SiteUserData",
    "TorrentIndex",
    "UploadTask",
]


class Base(DeclarativeBase):
    pass


def _bj_now() -> str:
    """北京时间字符串，替代已废弃的 ``_utcnow()``。"""
    from app.core.timeutils import bj_now
    return bj_now()


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
    #: 调度作业 ``cross_seed_maintenance``（cron ``17 */3 * * *``）扫描所有
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
    adapter: Mapped[str] = mapped_column(String(64), default="generic")

    # 认证与凭据
    rss_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    apikey: Mapped[str | None] = mapped_column(Text, nullable=True)
    cookies: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    #: 熔断截止时刻（北京时间字符串，NULL = 未熔断）。
    #:
    #: 站点返回 403/429（限流 / 封禁）时写入，见
    #: :class:`app.adapters.sites.base.BreakerTripped`。熔断期内
    #: 拉取作业**跳过该站且不发请求**——继续请求只会加速账号暴露。
    #:
    #: 历史：FR-2.2 原设计把熔断状态存在 ``feeds.breaker_until``，
    #: 该表已下线；``feeds`` 属于「种子条目」而非「站点配置」，
    #: 熔断是**站点级**状态，故落在本表。
    breaker_until: Mapped[str | None] = mapped_column(String(32), nullable=True)

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


class AppEnvSetting(Base):
    """环境变量配置（DB 持久化的字符串型设置）。

    与 :class:`AppRuntimeParam` 的分工：

    - :class:`AppRuntimeParam` —— **整型**作业运行参数（``value_int``），
      归属作业，按作业的「配置」弹框分组编辑
    - 本表 —— **字符串型**进程级配置（辅种扫描目录 / 未来其它 env→DB
      的字符串设置），不归属作业，在「设置 → 环境变量」tab 集中编辑

    单行 kv 表（PK = key）。仅存**用户显式改过**或**首次启动回填**的
    配置——未在此表出现的配置用 env 来源值（``AppSettings`` 同名字段）。
    因此新增配置项时不会受历史行影响。

    为何独立于 :class:`AppSettings`：

        :class:`AppSettings` 是启动期一次性加载的 pydantic 配置，
        **不可运行期修改**。本表把 env 来源的字符串配置落库，让用户在
        Web 页面的修改跨重启保持（与 ``scheduler_job_settings`` 同构）。
        详见 :mod:`app.services.env_settings`。
    """

    __tablename__ = "app_env_settings"

    #: 配置名（PK）—— 与 ``AppSettings`` 同名字段一致
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 配置值（字符串型；多目录等以逗号分隔存原值，由业务侧 parse）
    value: Mapped[str] = mapped_column(Text, default="")
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


class SchedulerJobSetting(Base):
    """调度作业的持久化调度配置。

    :class:`~app.services.scheduler.JobSpec.enabled` 只存在于内存，容器重启
    后作业会被 :func:`~app.services.job_registry.register_jobs` 重新注册为
    ``enabled=True``——用户在 UI 里手动停用的作业会"复活"。

    本表把调度配置落库，启动期回填，让用户的选择跨重启保持。

    两列都是**可选覆盖**（可空 / 有默认值）：

    - ``enabled`` —— 是否启用（False = 停用，不参与 cron 调度）
    - ``cron`` —— cron 表达式（NULL = 用代码里的默认值）

    只存**用户显式改过**的作业：未在此表出现的作业用注册时的默认值
    （``enabled=True`` + 代码里的 cron），因此新增作业不会被历史行影响。
    """

    __tablename__ = "scheduler_job_settings"

    #: 作业名（PK），与 :class:`~app.services.scheduler.JobSpec.name` 一致
    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 是否启用（False = 停用，不参与 cron 调度）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: cron 表达式（5 字段：分 时 日 月 星期）。NULL = 用代码默认值。
    #:
    #: 页面「配置」弹框改的 cron 落在这里——**跨重启保持**。改 env 无效
    #: （cron 不再从 env 读）。
    cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


class SchedulerJobSettingBackup(Base):
    """调度作业配置的**删除快照**（不可逆迁移的恢复依据）。

    启动期的作业合并 / 改名 / 孤儿清理会 ``DELETE``
    :class:`SchedulerJobSetting` 的旧行——用户自定义的 cron 与启停状态
    删掉就没了。本表在**同一个事务**里留下被删行的最后一份内容：

    - 比启动日志可靠（日志会轮转、会随容器重建而丢）；
    - 能用 SQL 直接还原（恢复步骤见 ``CHANGELOG.md`` 的「回滚 / 恢复」）。

    快照写失败 → 整个事务回滚 → 旧行**不删**（下次启动重试），
    不会出现「删了却没记录」。

    每个作业名一行（PK）——同一作业被反复删除时覆盖上一份（保留最新）。
    """

    __tablename__ = "scheduler_job_settings_backup"

    #: 作业名（PK），与 :class:`SchedulerJobSetting.job_name` 一致
    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 删除前的启停状态
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 删除前的 cron（NULL = 用代码默认值）
    cron: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 删除原因（合并到新作业 / 清理已下线作业 / 迁移到新作业）
    reason: Mapped[str] = mapped_column(String(64), default="")
    #: 删除时刻（北京时间字符串）
    deleted_at: Mapped[str] = mapped_column(String(32), default=_bj_now)


class CrossSeedSnapshot(Base):
    """辅种下载器扫描快照（定时抓取结果）。

    ``cross_seed_maintenance`` 作业（cron ``17 */3 * * *``）定时拉取
    ``is_cross_seed=True`` 的下载器全量种子，把统计结果 upsert 到本表
    ——**每个辅种下载器一行**（``downloader_id`` UNIQUE）。

    为什么落库：辅种下载器的种子量大（实测 3000+ 个），实时调下载器
    接口要 20s+。前端每次打开辅种页面都打一次实时接口既慢又给下载器
    加压。故改为「定时抓取 + 读快照」。

    与 :class:`SiteUserData` 同构：**当前快照**，不做历史（需要时间序列
    时再建 ``cross_seed_snapshot_history`` 表再迁）。

    **FK 不带 ``ON DELETE CASCADE``**——故意让它当守卫：删下载器时必须
    显式删快照行，漏了会 IntegrityError（宁可失败也不留孤儿行）。
    删除逻辑见 :func:`app.services.downloader_service.delete_downloader`。
    """

    __tablename__ = "cross_seed_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 关联辅种下载器（一站一行）
    downloader_id: Mapped[int] = mapped_column(
        ForeignKey("downloader_instances.id"), unique=True, index=True
    )
    #: 下载器名（冗余存一份：列表展示免 JOIN，且下载器改名后仍看得到历史名）
    downloader_name: Mapped[str] = mapped_column(String(64), default="")
    #: 扫描时刻（北京时间字符串）
    scanned_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    #: 种子总数
    total_torrents: Mapped[int] = mapped_column(Integer, default=0)
    #: 唯一分组数（按 name + size 精确分组）
    unique_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 异常种子数（tracker 未注册 / 数据丢失）
    anomaly_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 在途任务数（非暂停、非致命错误；口径见 ``TorrentState.is_inflight``）
    inflight_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 本次**实际**发出暂停指令的种子数（不含已暂停被跳过的）
    paused_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 暂停失败的种子数
    pause_failed_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 磁盘剩余空间（字节）。NULL = 该下载器不提供 / 本次没取到。
    #:
    #: qBittorrent 只给剩余空间（``sync/maindata`` 的
    #: ``free_space_on_disk``），Transmission 给剩余 + 总量。
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 磁盘总量（字节）。NULL = 拿不到（qBittorrent 不提供）。
    disk_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 种子**声明占用**字节数（各种子 ``completed_bytes`` 求和）。
    #:
    #: ⚠️ **不等于磁盘真实占用**：辅种的本质是**同一条数据被多个种子共享**
    #: （实测辅种站 4646 个种子只对应 2237 份数据），共享部分被重复计了
    #: N 次——故它可以**大于**磁盘真实占用（实测 71.47 TB vs 分区 26 TB）。
    #: 真实磁盘占用 = ``disk_total_bytes - disk_free_bytes``。
    #:
    #: 另：TR 的 ``haveValid`` 是**单个种子**的精确磁盘占用；qB 的
    #: ``completed`` 是 ``total_wanted_done``——**不含** unwanted 文件与
    #: 冗余块，故 qB 的这个值会**小于**真实磁盘占用。
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 种子**总大小**字节数（各种子 ``size`` 求和，理论值）——同样含共享重复计入。
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 按下载目录分布（JSON 数组：
    #: ``[{dir, count, bytes, total_bytes}]``，bytes = 声明占用）
    by_dir: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    #: 分组统计明细（JSON 数组：``[{name, size_gb, size_bytes, count,
    #: sample_hash, hashes, added_at}]``）。
    #:
    #: - ``size_bytes``：精确字节数（2026-11-21 加）——``size_gb`` 只有两位
    #:   小数，disk_rotation 的删除预算/释放量记账按它算会累积舍入误差；
    #:   旧快照行没有该 key，消费方回退 ``size_gb``（优雅降级）
    #: - ``added_at``：``{hash: unix秒}`` 映射，只存有时间的种子
    #:   （disk_rotation 同档内先删旧的）
    #: - ``hashes``：完整 40 位 infohash 列表——**删除以组为单位**，整组
    #:   一次下发（只删一半既释放不了空间，还会让余下副本 missingFiles）
    stats: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    #: 异常种子明细（JSON 数组：``[{id, hash, name, size_gb, status,
    #: anomaly_type, error_code, error_string}]``）
    anomalies: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


class CrossSeedOrphan(Base):
    """辅种**孤儿文件**统计（文件在、任务没了）。

    辅种站的下载目录通过 NFS/CIFS **只读**挂载到 seedhelper 容器，作业
    定期扫盘，把「磁盘上有、但下载器里已经没有对应任务」的顶层条目
    统计成孤儿。

    **为什么需要**：用「仅删任务」的方式清理种子时，磁盘文件会保留下来
    ——它们不再被做种，却一直占着空间（辅种站实测 21 个孤儿占 527 GB）。
    下载器**没有**列目录的接口（TR 的 RPC spec 不提供 filesystem 浏览，
    qB 也没有），故只能靠挂载目录 + 顶层名匹配来发现。

    每个辅种下载器一行（``downloader_id`` UNIQUE）。
    """

    __tablename__ = "cross_seed_orphan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 关联辅种下载器（一站一行）
    downloader_id: Mapped[int] = mapped_column(
        ForeignKey("downloader_instances.id"), unique=True, index=True
    )
    #: 下载器名（冗余存一份：列表展示免 JOIN）
    downloader_name: Mapped[str] = mapped_column(String(64), default="")
    #: 扫描时刻（北京时间字符串）
    scanned_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    #: 孤儿条目数（顶层目录/文件个数）
    orphan_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 孤儿占用总字节数
    orphan_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 扫描目录的总字节数（用于算占比）
    total_scan_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 扫描目录里的顶层条目总数
    total_scan_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 孤儿明细（JSON 数组：
    #: ``[{name, path, bytes, file_count, scan_dir}]``）
    orphans: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    #: 空目录孤儿数（0 文件）——它们是残留但**不占空间**
    empty_dir_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 逐目录的扫描统计（JSON 数组：
    #: ``[{scan_dir, total_count, orphan_count, orphan_bytes,
    #: empty_dir_count, owned_count}]``）
    scanned_dirs: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list
    )
    #: 扫描不可用时的原因（NULL = 扫描成功）
    #: 例：目录未挂载 / 不可读 ——**不静默失败**，前端据此显示原因
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


class SiteUserData(Base):
    """站点用户数据快照（FR-1.6 站点用户数据契约·精简版）。

    站点页「全部测连」/ 单站测连时调一次各适配器的
    :meth:`SiteAdapter.get_user_snapshot`，把 3 业务字段
    upload / download / seeding_count upsert 到本表（``site_id`` UNIQUE）。

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
    #: 最近一次刷新时间（北京时间）
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


# ================================================================ 媒体库域
#
# 以下 8 张表来自 strmmanger。时间列已与辅种域统一为北京时间字符串
# （``String(32)``，:func:`_bj_now`）；协议侧时刻（``link_cache.expires_at``）
# 保留 unix 秒，见模块 docstring 与各列注释。

class Driver(Base):
    """存储源（网盘账号等）+ 其媒体库配置（**一对一**）。

    配置直接放在本表——「一个存储源 = 一套媒体库配置」，两张表纯属冗余。
    """

    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 驱动类型标识，如 ``guangya``。后续加 ``aliyun`` / ``quark`` / ``webdav``
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 展示名
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 加密后的凭据（光鸭 token 三件套等）。密文格式自带 scheme 前缀。
    credential: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 创建时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    # -- 媒体库配置（与存储源一对一）--

    #: 媒体库目录 id（光鸭是 fileId，**字符串**）。
    #:
    #: 用 id 而非路径：网盘的目录 id 在改名后**不变**，路径会失效
    #: （反查接口会抛「找不到」）。且 id 只需 1 次
    #: ``iterdir``，路径要 ``dir_getid`` + ``iterdir``。
    media_cid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 媒体库路径（**只读展示**）——保存时用 cid 实时解析填入
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 本地媒体库目录（STRM 输出根）
    output_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 视频文件后缀（逗号分隔，如 ``mkv,mp4``）——**扫描依据**
    video_exts: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_VIDEO_EXTS
    )
    #: 媒体图片后缀（逗号分隔）——**附件同步**（海报/剧照下载到本地，
    #: 与 strm 同目录）；清空 = 不同步图片
    image_exts: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_IMAGE_EXTS)
    #: 媒体数据后缀（逗号分隔）——**附件同步**（nfo / 字幕下载到本地）；
    #: 清空 = 不同步元数据
    data_exts: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_DATA_EXTS)
    #: 最小视频体积（MB）——小于此值的文件跳过（过滤样片）；0 = 不过滤
    min_size_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 是否包含子目录（cid 指向媒体库根时，是否递归其下所有子目录）
    include_subdirs: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    #: 扫描时是否**自动清理**「网盘上已消失」的条目（prune）。
    #:
    #: 默认关：清理会**永久删除**本地 strm（不进回收站），不可逆。
    #: 关着时扫描只加不删——网盘删了文件，DB 与本地 strm 会一直留着。
    #: 手动 ``?prune=true`` 仍可单次强制清理（覆盖本配置）。
    auto_prune: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: 上次扫描时间（北京时间字符串，NULL = 从未扫描）
    last_scan_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 上次扫描新增/更新的条目数
    last_synced_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # -- 聚合（主从）配置 --

    #: 所属聚合组（NULL = 独立存储源，不参与聚合）。
    #:
    #: **一盘一组**：不允许跨组嵌套——否则归拢链路会成环、额度归属混乱。
    aggregate_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("aggregate_groups.id"), nullable=True
    )
    #: 组内角色（``master`` / ``secondary``；NULL = 不参与聚合）。
    #:
    #: - ``master`` —— 主数据源：内容以它为准（扫描 / STRM / 播放只认它）
    #: - ``secondary`` —— 副数据源：缓冲，**内容不对外展示**
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: **本月上传额度已耗尽**的月份键（``"2026-11"``；NULL = 未耗尽）。
    #:
    #: 光鸭的「每月上传限额」**没有查询接口**——只能撞墙探测（与聚合的
    #: 「云添加额度」同一套哲学）。撞到限额类报错时写上当前月份，
    #: 落点分配器据此跳过该账号；**跨月自动失效**（键 ≠ 当前月即正常）。
    upload_quota_exhausted_month: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    __table_args__ = (
        Index("idx_drivers_group_role", "aggregate_group_id", "role"),
    )


class AggregateGroup(Base):
    """聚合组：1 个主数据源 + N 个副数据源。

    **组才是聚合的单位**——不同组互不影响（G1 的主盘额度耗尽不影响 G2 归拢）。
    组外独立盘（``drivers.aggregate_group_id IS NULL``）行为与引入聚合前**完全
    一致**（不参与任何自动流程）。
    """

    __tablename__ = "aggregate_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 组名（用户可读）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 是否启用（停用的组不参与归拢调度）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: **归拢目标目录 id**（主盘上的目录，光鸭 fileId）。
    #:
    #: 归拢落点是**组自己的配置**——不是主盘的媒体库目录（那是存储源的
    #: 媒体库配置，与聚合无关）。副盘缓冲根下的相对路径原样铺到它下面。
    target_dir_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 归拢目标目录路径（**只读展示**）
    target_dir_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 创建时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )


class AggregateGroupRoot(Base):
    """组 ↔ 副盘**缓冲根目录**的映射（每个副盘一行）。

    **为什么是组级而不是挂在存储源上**：「从副盘的哪个目录，搬到主盘的哪个
    目录」是一条**组与组之间的映射关系**，两端都属于组。挂在 ``drivers``
    上会把聚合的配置混进存储源配置（存储源那边管的是媒体库：媒体库目录 /
    本地输出目录 / 后缀 / prune）。

    ⚠️ **归属**（``drivers.aggregate_group_id`` / ``drivers.role``）仍然
    挂在盘上——那是 1:1 的归属关系（一个盘一个组、组内一个角色），无处可挂。
    """

    __tablename__ = "aggregate_group_roots"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 所属聚合组
    group_id: Mapped[int] = mapped_column(
        ForeignKey("aggregate_groups.id", ondelete="CASCADE"), nullable=False
    )
    #: 副数据源（缓冲根在这个账号里）
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    #: 缓冲根目录 id（该副盘账号内的 fileId）
    root_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 缓冲根目录路径（**只读展示**）
    root_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 创建时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    __table_args__ = (
        UniqueConstraint("group_id", "driver_id", name="uq_group_root_driver"),
    )


class SecondaryStaging(Base):
    """副盘在途台账：一个待归拢的文件。

    生命周期（``state``，取值见 :mod:`app.domain.aggregate_types`）::

        pending → syncing → verified → done
           ↓          ↓         ↓
        failed     paused    （人工介入 / 次日续传）

    **副盘内容永不对外展示**——这张表是「在路上」的唯一可见形态。
    """

    __tablename__ = "secondary_staging"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 所属聚合组
    group_id: Mapped[int] = mapped_column(
        ForeignKey("aggregate_groups.id", ondelete="CASCADE"), nullable=False
    )
    #: 副数据源（文件当前所在盘）
    source_driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    #: 主数据源（文件要归拢到哪）
    target_driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    #: 副盘上的文件 id（光鸭 fileId）
    remote_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 文件名（含扩展名）
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: 副盘上的相对目录（用于定位文件 + 组装目标路径）
    source_dir: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 目标目录（相对主盘归拢根目录）
    target_dir: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 文件字节数
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: 状态（见 :mod:`app.domain.aggregate_types`）
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    #: 搬运通道（``cloud_download`` / ``share_transfer``）。
    #:
    #: 小文件走「分享转存」（服务端内部复制，**不消耗云添加额度**），
    #: 大文件走「离线下载」（云下载，消耗额度）——看板要能分开统计。
    transfer_mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: 已尝试次数
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 最近一次失败原因（用户可见）
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 在途的云下载任务 id（中断恢复用）
    cloud_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 创建时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )
    #: 归拢完成时间（北京时间字符串）
    synced_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 副盘副本清理完成时间（北京时间字符串）
    cleaned_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_driver_id",
            "remote_id",
            name="uq_staging_source_remote",
        ),
        Index("idx_staging_group_state", "group_id", "state"),
    )


class MediaFile(Base):
    """媒体条目：一条 STRM 对应一个存储源文件。"""

    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    #: 存储源内的文件标识（光鸭为 fileId）。不出现在对外 URL 里。
    remote_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 完整路径（存储源内）
    path: Mapped[str] = mapped_column(Text, nullable=False)
    #: 文件名（含扩展名，不含目录）
    name: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: STRM 文件落盘路径（宿主机绝对路径）
    strm_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 中转 URL 的 HMAC 签名（密钥轮换时批量重算）
    signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 源文件 mtime（北京时间字符串，用于增量扫描判断是否变更；
    #: 由网盘返回的 unix 秒经 ``format_bj`` 转换写入）
    mtime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    __table_args__ = (
        UniqueConstraint("driver_id", "remote_id", name="uq_media_files_driver_remote"),
        Index("idx_media_files_driver_path", "driver_id", "path"),
        Index("idx_media_files_strm_path", "strm_path"),
    )


class LinkCacheEntry(Base):
    """本地直链缓存（L2，持久化）。

    主键 ``(media_id, ua_hash)``——UA 分桶是正确性要求，不是性能优化。

    表名 ``link_cache``；类名带 ``Entry`` 后缀以避开服务层
    :class:`app.services.link_cache.LinkCache` 的命名冲突。

    ``media_id`` **不建外键**：缓存是派生数据，媒体条目被删除后残留的
    缓存行应能被独立清理；加外键会让「删条目」必须先删缓存，徒增耦合。
    """

    __tablename__ = "link_cache"

    media_id: Mapped[int] = mapped_column(primary_key=True)
    #: User-Agent 的哈希（不存明文：UA 可能含设备指纹）
    ua_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    #: 过期时间戳（unix 秒，已扣除安全余量）——**协议侧时刻**，保留 Integer
    #: 与 ``time.time()`` 直接比较；不参与「DB 时间列统一为北京时间字符串」。
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 创建时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)

    __table_args__ = (Index("idx_link_cache_expires", "expires_at"),)


class Setting(Base):
    """KV 配置（业务级配置，不走环境变量）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    #: 最近一次修改时间（北京时间字符串）
    updated_at: Mapped[str] = mapped_column(String(32), default=_bj_now)


class DavUser(Base):
    """WebDAV 账号。

    **与面板账号分离**：面板是单管理员（``WEB_USERNAME``/``WEB_PASSWORD``
    走环境变量），WebDAV 账号是「给播放器用的凭证」——它们不该能开面板，
    面板也不该暴露给拿到播放器的第三方。

    **密码哈希**：PBKDF2-HMAC-SHA256（600k 次迭代），格式与
    :mod:`app.core.auth` 的面板密码一致，便于复用校验逻辑。
    """

    __tablename__ = "dav_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 登录名（唯一）
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: PBKDF2 哈希：``pbkdf2$<iters>$<salt_hex>$<dk_hex>``
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: 是否启用（停用 = 立即失效，不用删账号）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 上次登录时间（北京时间字符串，NULL = 从未登录）
    last_login_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    __table_args__ = (Index("idx_dav_users_username", "username"),)


class DavAuditLog(Base):
    """WebDAV 请求审计。

    **只记「谁动了什么」**，不记所有 HTTP 请求 —— 否则 Infuse 扫一次库
    就是几百条，表会迅速膨胀。

    粒度：

    - ``GET`` / ``HEAD`` —— **逐条明细**（谁拿了哪个文件）
    - ``PROPFIND`` —— **汇总**一条（带 ``item_count``）
    - 认证失败 —— 逐条（IP + 用户名 + UA）
    """

    __tablename__ = "dav_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 用户 id；认证失败时用户可能不存在 → 0
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 当时的用户名（**快照**——用户被删后审计仍可读）
    username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: HTTP 方法（PROPFIND / GET / HEAD / OPTIONS）
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    #: 请求路径（相对 DAV 根）
    path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: HTTP 状态码
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 客户端标识（User-Agent，截断）
    client: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: 来源 IP
    remote_addr: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 处理耗时（毫秒）
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: PROPFIND 的条目数（明细为 0）
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 审计时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)

    __table_args__ = (
        Index("idx_dav_audit_user_time", "user_id", "created_at"),
        Index("idx_dav_audit_time", "created_at"),
        Index("idx_dav_audit_path", "path"),
    )


class UploadTask(Base):
    """上传任务：把本地目录的文件同步到光鸭盘。

    **一个任务 = 一个监听目录 + 一个光鸭目标目录**。

    刻意**不建上传记录表**（每个文件一行）——与「迁移」同哲学：
    **光鸭目标目录就是唯一真相源**。幂等判断用「同名 + 同大小」即可，
    不依赖本地状态，容器重启不丢进度。代价是「本地文件删了」这类信息
    无从察觉（设计取舍：本地删除**不联动**云端，见 ``docs/upload.md``）。

    **监听目录没有白名单**——完全由用户在面板上配（目录浏览器逐层选）。
    故 ``watch_root`` 是**完整路径**（不再拆「根 + 子路径」）。
    """

    __tablename__ = "upload_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 任务名（展示用）
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 目标存储源（**必须是 guangya**）。删存储源时级联删任务。
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    #: 光鸭目标目录的 fileId
    target_file_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 光鸭目标目录路径（**只读展示**——光鸭没有「路径 ↔ id」反查）
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 监听目录（容器内绝对路径，**完整路径**——不再拆根 + 子路径）
    watch_root: Mapped[str] = mapped_column(Text, nullable=False)
    #: 是否递归子目录
    include_subdirs: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 是否启用（停用 = 不监听，但任务配置保留）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 同步成功后是否清理本地文件。
    #:
    #: **破坏性操作**——故有三重保护：
    #:
    #: 1. 只在**云端大小与本地一致**时才删（:func:`app.services.uploader.
    #:    _check_cleanup_allowed`：云端 fileId 非空 + 大小相等）
    #: 2. 云端大小不一致 → **不删**（本地是不同版本，原件留着）
    #: 3. 移进**回收目录**（``<监听根>/.strmmanger-trash/``）而不是 ``unlink``
    #:    —— 可手动还原
    #:
    #: 注：``unchanged``（云端已有同名同大小 = 之前传过）**也会删本地**
    #: （2026-10-31 修正：云端已是完整副本 → 本地是冗余）。「冲突 /
    #: 云端更大 / 上传失败」一律保留本地。
    #:
    #: 默认 ``False``：新任务默认不删本地，需要用户显式开启。
    delete_after_upload: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: 同时进行的上传数（**语义 = 小文件通道并发数**，大文件通道恒串行；
    #: 默认值单一真源见 :data:`app.domain.upload_types.DEFAULT_UPLOAD_CONCURRENCY`）
    concurrency: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_UPLOAD_CONCURRENCY, nullable=False
    )
    #: 同名且**大小不同**时的冲突策略：
    #:
    #: - ``0`` = 本地优先：删掉云端旧的，传本地的（**历史行为**）
    #: - ``1`` = **保留大的**（默认）：本地比云端大才覆盖；本地更小则跳过上传，
    #:   保留云端那份（本地原件也会因「云端大小 ≠ 本地大小」而不被清理）
    #:
    #: 默认 ``1``：小文件误覆盖大文件是真实踩过的坑（本地重新压制出的
    #: 版本往往更小）。``0`` 保留给「本地永远是最新」的工作流。
    conflict_policy: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: 目标**聚合组**（NULL = 目标就是 ``driver_id`` 那个存储源）。
    #:
    #: 非空 = 「本地同步到网盘聚合」：落点由**落点分配器**在组内副盘之间
    #: 自动挑选（撞到月度上传限额就换下一个），上传成功后**即时登记台账**，
    #: 归拢引擎接管搬运（见 :mod:`app.services.aggregate_upload`）。
    #:
    #: ⚠️ 此时 ``driver_id`` / ``target_file_id`` 是**分配器维护的落点快照**
    #: （粘性——重启后恢复，避免已传文件被重传），不是用户直接指定的目标。
    aggregate_group_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    #: 目标**子目录**（聚合模式专属）：**组映射的子路径覆盖**。
    #:
    #: 非空 = 文件最终落在主盘「归拢目标目录」下的这个子目录里
    #: （例：``电影/外语``）。实现上落在**副盘缓冲根**下的同名子目录——
    #: 归拢引擎「相对结构原样保留」，主盘侧自动带上前缀。
    #:
    #: NULL = 直接落在归拢目标目录根（引入本字段前的行为，存量行全是
    #: NULL → 行为零变化）。「指定存储源」模式下恒为 NULL（没有组映射
    #: 可覆盖）。
    aggregate_sub_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    # 注：**没有**「上次同步时间 / 上次上传数 / 上次错误」列——
    # 统计一律是**内存运行态**（``UploadRuntime``），重启后重跑即可，
    # 幂等靠云端目录（同名 + 同大小 = 已同步），不依赖本地状态。
    # 这三列曾长期「只写不读」（前端只渲染运行态），写进 DB 只是给
    # SQLite 单写者添堵——已随 2026-11-02 的清理删除（见 bootstrap）。

    __table_args__ = (Index("idx_upload_tasks_driver", "driver_id"),)


class ProbeResult(Base):
    """存储源可用性探测结果（登录态、容量等）。"""

    __tablename__ = "probe_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    used_gb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_gb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 探测时间（北京时间字符串）
    probed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("idx_probe_results_driver", "driver_id"),)


class MediaScrape(Base):
    """刮削结果（当前快照，不做历史）。

    **粒度是「刮削单元」而不是「文件」**：电影的单集 NFO、剧集的
    ``tvshow.nfo``、季目录的 ``season.nfo`` 都不是按视频文件存的，而是按
    **产物落点**存的。因此用 :attr:`scope_key` 一列表达唯一性：

    ==========================  ==========================================
    scope_key                   含义
    ==========================  ==========================================
    ``movie|<目录>``            电影（目录 = 视频所在目录）
    ``tvshow|<剧集根目录>``      电视剧整剧
    ``season|<季目录>|S1``       季
    ``episode|<目录>|S1E2``     单集
    ==========================  ==========================================

    **为什么不用外键挂 media_files**：刮削对象是**本地媒体库目录**里的
    产物，与网盘条目（``media_files``）没有从属关系——整理产物先落在本地，
    再由数据同步传上网盘。两者的生命周期不同，强行挂 FK 会让「网盘条目
    删了」误删本地刮削记录。

    **失败必须点名**：:attr:`error` 记录失败原因，:attr:`status` 为
    ``failed``——不写「空结果兜底」（那会把故障伪装成「没内容」）。
    """

    __tablename__ = "media_scrape"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 刮削单元唯一键（见类文档的取值表）
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: 刮削层级：movie / tvshow / season / episode
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 结果状态：scraped / failed / skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 媒体类型：movie / tv
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: TMDB 媒体 ID
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 标题（TMDB 原标题）
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 年份
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 季号（季 / 单集层级有）
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 集号（单集层级有）
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: NFO 落盘路径（绝对路径）
    nfo_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 被刮削的**媒体文件**路径（绝对路径）——「一键修正」靠它定位原文件
    media_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 已写出的图片清单（JSON 数组：``[{type, path}]``）
    images: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    #: 失败原因（status = failed 时有值）
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 刮削时刻（北京时间字符串）
    scraped_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    __table_args__ = (
        UniqueConstraint("scope_key", name="uq_media_scrape_scope"),
        Index("idx_media_scrape_status", "status"),
    )


class ScrapeCache(Base):
    """识别缓存（标题 + 年份 → TMDB 命中结果）。

    **为什么落库而不是进程内缓存**：整理作业与刮削作业是**分开跑的**，
    进程内缓存在两次作业之间就失效了；同一个种子被整理后紧接着刮削，
    会重复打 TMDB 搜索。

    ``expires_at`` 是**协议侧时刻**（unix 秒 Integer），与
    :class:`LinkCacheEntry` 同口径——它与 ``time.time()`` 直接比较。
    """

    __tablename__ = "scrape_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 缓存键：``<媒体类型>|<标题>|<年份>``
    cache_key: Mapped[str] = mapped_column(String(512), nullable=False)
    #: 命中结果（JSON：``{tmdb_id, media_type, title, year, ...}``）
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    #: 过期时刻（unix 秒）
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 创建时间（北京时间字符串）
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)

    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_scrape_cache_key"),
        Index("idx_scrape_cache_expires", "expires_at"),
    )


class ScrapeAlias(Base):
    """识别别名（标题 → TMDB ID 的固定映射）。

    **为什么需要**：TMDB 的条目名与资源站给的英文名不一定一致，且中文
    译名有时很机械（《被放逐的作弊赋予魔术师享受随心所欲的第二人生。》）。
    识别只能靠 popularity 猜——猜错时唯一手段是「每次手动填 tmdb_id 重刮」，
    治标不治本。别名表把修正**固化**下来：同一个标题下次直接命中。

    ``alias_key`` 是**归一化**后的标题（去空白与标点、小写）——「Fight
    Club」与「fight club」视为同一条，避免重复条目。
    """

    __tablename__ = "scrape_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 归一化标题（唯一键）
    alias_key: Mapped[str] = mapped_column(String(512), nullable=False)
    #: 原始标题（展示用——归一化后可能不好读）
    alias_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: 目标 TMDB ID
    tmdb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 媒体类型：movie / tv（NULL = 不限）
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 备注（为什么改）
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    __table_args__ = (
        UniqueConstraint("alias_key", name="uq_scrape_alias_key"),
    )


class OrganizeHistory(Base):
    """整理历史（每个源文件一条，用于判重与审计）。

    **输入是「源目录」不是「下载器任务」**——整理与下载器解耦：文件从哪来
    无所谓（BT 下载、手动拷贝、其它工具），把视频放进源目录就会被扫到。

    **判重键是 ``source_path``**：同一个源文件只整理一次。重跑作业时靠这个
    键跳过——比「目标文件是否存在」可靠（目标可能被用户手动删掉，但历史
    仍在，此时应当**重新整理**而不是静默跳过）。

    ``status`` 为 ``failed`` 时 :attr:`error` 记录原因，**失败必须点名**。
    """

    __tablename__ = "organize_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 源文件绝对路径（判重键）
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    #: 目标文件路径（本地媒体库内）
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 整理动作：copy / move
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="copy")
    #: 结果状态：success / failed / skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 媒体类型：movie / tv
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: TMDB 媒体 ID
    tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 标题
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 年份
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 季号
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 集号
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 失败原因
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 整理时刻（北京时间字符串）
    organized_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )

    __table_args__ = (
        UniqueConstraint("source_path", name="uq_organize_history_source"),
        Index("idx_organize_history_status", "status"),
    )
