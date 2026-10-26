"""ORM 模型（§8 关键数据模型）。

M1 建立全部表结构（M2~M5 只填充逻辑）。时间列统一北京时间字符串。

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

__all__ = [
    "AppEnvSetting",
    "Base",
    "CrossSeedOrphan",
    "CrossSeedSnapshot",
    "DownloaderInstance",
    "JobRun",
    "SchedulerJobSetting",
    "Site",
    "SiteUserData",
    "TorrentIndex",
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


class CrossSeedSnapshot(Base):
    """辅种下载器扫描快照（定时抓取结果）。

    ``scan_cross_seed`` 作业（cron ``2,32 * * * *``）定时拉取
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
    #: 分组统计明细（JSON 数组：``[{name, size_gb, count, sample_hash}]``）
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
    """站点用户数据快照（FR-1.6 站点用户数据契约·精简版 + 时魔）。

    ``refresh_user_data`` 作业每 30 分钟调一次各适配器的
    :meth:`SiteAdapter.get_user_snapshot`，把 4 业务字段
    upload / download / seeding_count / leeching_count
    以及时魔（hourly_bonus 系列）upsert 到本表（``site_id`` UNIQUE）。

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
    #: 时魔：每小时获得的站点积分（**合计口径**，含各项加成）
    hourly_bonus: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分）
    hourly_bonus_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 是否已达该站的获取上限（``None`` = 该站页面不提供上限信号）
    hourly_bonus_capped: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: 站点进度百分比（>=100 表示封顶；口径由站点自己定）
    hourly_bonus_progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 最近一次刷新时间（北京时间）
    last_seen_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    created_at: Mapped[str] = mapped_column(String(32), default=_bj_now)
    updated_at: Mapped[str] = mapped_column(
        String(32), default=_bj_now, onupdate=_bj_now
    )
