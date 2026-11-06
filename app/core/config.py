"""配置中心（FR-7）。

配置来源优先级：环境变量 > 数据库。

本模块只负责 **进程级启动配置**（环境来源），站点与下载器的业务配置
存于数据库，由 :mod:`app.db` 与各服务读取，不走 pydantic-settings。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """应用启动配置。所有字段均可用同名大写环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- 基础 --
    app_name: str = "SeedHelper"
    env: Literal["dev", "prod", "test"] = "prod"
    log_level: str = "INFO"
    log_json: bool = Field(
        default=False, description="结构化 JSON 日志；容器环境建议开启"
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="必须与站点规则时区一致，否则保种时长/窗口计算会偏差（§9.2）",
    )

    # -- 路径 --
    data_dir: Path = Path("/app/data")
    log_dir: Path = Path("/app/logs")
    #: 辅种站下载目录的**容器内**扫描路径列表（孤儿文件统计用）。
    #:
    #: 逗号分隔的**多个**目录，每个目录独立扫描 + 独立统计。
    #:
    #: 辅种数据在另一台机器（192.168.0.172 的 NAS）上，seedhelper 容器
    #: 看不到它——除非把那个目录**只读**挂载进来。下载器本身没有浏览
    #: 文件系统的接口（TR 的 RPC spec 不提供 filesystem 浏览，qB 也没有
    #: 列目录 API），故只能靠挂载 + 名称匹配来发现孤儿文件。
    #:
    #: **路径必须与下载器里的 downloadDir 完全一致**（TR 的 downloadDir
    #: 原值，如 ``/share/Public/home/downloads/辅种``）。这样任务与磁盘
    #: 条目才能按 downloadDir 分组对应——「这个目录归哪些任务管」决定了
    #: 哪些磁盘条目算「有主」。路径映射一旦有偏差就会误判。
    #:
    #: 留空 = 不做孤儿扫描（该功能降级，前端显示「未启用」）。
    cross_seed_scan_dirs: str = ""

    # -- Web 面板（FR-6 / 非功能·安全性）--
    # 注：``web_host`` / ``web_port`` 曾在此处配置监听地址与端口，但
    # **从未被读取**——容器入口（Dockerfile CMD）直接写死
    # ``uvicorn --host 0.0.0.0 --port 8080``。留着只会让人以为改这里
    # 能改监听端口（改了不生效），已删除。
    web_username: str = "admin"
    web_password: SecretStr | None = None
    web_token: SecretStr | None = None
    #: session cookie 是否带 Secure 标记（反代提供 HTTPS 时建议开启）
    web_cookie_secure: bool = False
    #: 允许的 CORS origins（逗号分隔）；空 = 不启用 CORS 中间件（同源部署）
    web_cors_origins: str = ""
    # -- 前端静态资源路径（FR-6.1 SPA 托管）--
    frontend_dist: Path = Path("/app/web/dist")

    # -- 数据库（§8 SQLite 约定）--
    db_path: Path | None = Field(default=None, description="默认 data_dir/medialab.db")
    db_journal_mode: Literal["WAL", "DELETE"] = "WAL"
    db_synchronous: Literal["NORMAL", "FULL", "OFF"] = "NORMAL"

    # -- 调度 --
    scheduler_enabled: bool = True
    #:
    #: 注：**cron 表达式与作业运行参数都不在这里**（2026-10-24 起）。
    #:
    #: - cron —— 默认值是代码常量（:data:`app.services.job_registry.DEFAULT_CRON`），
    #:   用户在页面改的落进 ``scheduler_job_settings`` 表，启动期回填覆盖
    #: - 运行参数 —— 落进 ``app_runtime_params`` 表，默认值见
    #:   :data:`app.services.runtime_params.PARAM_SPECS`
    #:
    #: 故 env 改这些**不再有任何效果**（且 ``extra="ignore"`` 会静默忽略
    #: 残留的旧行——它们只是被无视，不会报错）。
    #:
    #: 站点每日签到 cron（默认每天 08:37）。分钟位 37 刻意避开既有多 5
    #: 分钟的作业，避免同一分钟并发抢资源。

    # -- AI 读码（FR-3.6 站点签到的图形验证码）--
    # 三项**全为空则视为未配置**，需人工识别的站点会降级为 UNSUPPORTED
    # 而不是让作业变红。走 OpenAI 兼容协议（/v1/chat/completions）。
    ai_api_base: str = ""
    ai_api_key: SecretStr | None = None
    #: 必须是**视觉**模型（能吃 image_url 内容块）。纯文本模型读不了验证码。
    ai_model: str = ""
    ai_timeout_seconds: float = 60.0

    # ============================================================ 媒体库域
    #
    # 以下配置来自 strmmanger。两个域的变量名**零冲突**，可共存于同一个
    # ``.env``。

    # -- 中转服务（302 直链）--
    #: 对外可达地址。写进 STRM 文件的 URL 用它 —— 必须是**播放器能访问到的**
    #: 地址，不是容器内地址（否则 STRM 里的 URL 对客户端不可达）。
    public_base_url: str = Field(
        default="",
        description="对外可达的基础地址，如 http://192.168.1.10:8080；为空则用 host/port 拼",
    )
    #: STRM 中转 URL 的 HMAC 签名密钥。为空时启动报错（无鉴权的中转等于把网盘
    #: 取流能力开放出去）。
    signing_secret: SecretStr | None = None
    #: 直链缓存 L1 容量（进程内 LRU 条目数）
    link_cache_l1_size: int = Field(default=1000, ge=1)
    #: 直链缓存 TTL 安全余量（秒）：expires_at = parse(?t=) - margin
    link_cache_ttl_margin_sec: int = Field(default=300, ge=0)
    #: 直链 URL 无 ?t= 参数时的 fallback TTL（秒）
    link_cache_default_ttl_sec: int = Field(default=600, ge=1)
    #: 直链缓存 L2 过期清理间隔（秒）；0 = 禁用
    link_cache_cleanup_interval_sec: int = Field(default=600, ge=0)
    #: UA 策略：无 UA 客户端（Infuse 等）的兜底方式
    #:
    #: - ``strict``          严格按 UA 分桶，UA 不符则上游 403
    #: - ``fallback_default`` 无 UA 时用 :attr:`default_user_agent` 换直链
    #: - ``relaxed``         尽量取 ``f=0``（不绑定 UA）的直链
    ua_policy: Literal["strict", "fallback_default", "relaxed"] = "fallback_default"
    #: UA 策略为 ``fallback_default`` 时，用于无 UA 客户端的兜底 UA。
    #: 换直链与访问直链用同一个 UA 才有效（``f=1`` 的语义）。
    default_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # -- 请求限速（媒体库域）--
    #: 115 请求的最小间隔（秒）。0 = 不限速。
    #:
    #: 115 对高频请求会限流（表现为 ``405 Method Not Allowed``）。递归扫描
    #: 一棵大树要发几百个请求，不限速必然被打。默认 1.0 秒。
    rate_limit_interval_sec: float = Field(default=1.0, ge=0.0)

    #: 光鸭盘的最小间隔（秒）。0 = 不限速。
    #:
    #: 光鸭官方限频是 5 次/秒（列目录）/ 2 次/秒（换直链），比 115 宽松，
    #: 故单列一个值。默认 0.2 秒。
    guangya_rate_limit_interval_sec: float = Field(default=0.2, ge=0.0)

    #: 迁移时轮询光鸭离线任务状态的间隔（秒）。
    #:
    #: 一个共享 poller 服务所有并发 worker，故与并发数无关。默认 5 秒——
    #: 页面每 2 秒刷新一次，5 秒的粒度足够看出进度变化，又不会把光鸭打爆。
    migration_poll_interval_sec: float = Field(default=5.0, ge=1.0)

    # -- 光鸭盘 --
    #: 光鸭 Web 端的 client_id（官方固定值，不是用户申请的）。
    guangya_client_id: str = "aMe-8VSlkrbQXpUR"
    #: 账号域名（登录 / 刷新 Token）
    guangya_account_base_url: str = "https://account.guangyapan.com"
    #: 业务域名
    guangya_api_base_url: str = "https://api.guangyapan.com"

    # -- WebDAV（只读暴露媒体库目录）--
    #: WebDAV 是否启用。生产默认关——暴露前要先建好 DAV 账号。
    dav_enabled: bool = False

    #: DAV 挂载路径（FastAPI 侧）。必须是 ``/`` 开头、不以 ``/`` 结尾。
    dav_mount_path: str = "/dav"

    #: 按 IP 的请求数限流（每分钟）。0 = 不限流。
    #:
    #: 与面板**分开计** —— 面板是人点（60/min 够）。
    #:
    #: **阈值依据实测的媒体库规模**：生产库有 970 个目录 / 28384 个文件，
    #: 客户端（Infuse 等）完整扫一遍需要约 1000 次 PROPFIND。按 300/min
    #: 算要 4 分钟以上 —— 客户端会中途超时，看起来像「连不上」。
    #:
    #: 故默认 1200/min：完整扫库约 1 分钟内完成，同时仍能挡住
    #: 「一个 IP 狂刷几十万次」这种真正的攻击。**防爆破的主要防线是
    #: 认证失败锁定**（5 次 / 锁 15 分钟），不是这个限流。
    dav_rate_limit_per_min: int = Field(default=1200, ge=0)

    # -- 离线下载（本地文件 → 光鸭，走光鸭服务端拉取）--
    #
    # 光鸭的离线下载是**服务端主动来拉**——流量方向是**下行**，本机只
    # 承担接收，上行需求 ≈ 0。这绕开了「本机上行 ~3.3 MB/s」这个瓶颈
    # （见 ``docs/upload-bandwidth.md``）。
    #
    # 光鸭的 ``cloud_create_task(url, parent_id)`` **只收 URL、不支持
    # 自定义 Header** → 鉴权只能做在 URL 里（每文件一个 HMAC token）。
    #: 离线下载的 HMAC 密钥。**空 = 不启用**（不挂载本地文件服务）。
    #:
    #: **临时使用**：传完就关掉端口映射即可，不必改这个值——密钥只在
    #: URL 生成时起作用，映射关掉后外部就访问不到了。
    offline_hmac_secret: str = ""

    #: 离线下载暴露的根目录（容器内绝对路径）。空 = 用监听目录。
    offline_files_root: str = ""

    #: 离线下载对外的**公网入口**（光鸭用它拼 URL）。
    #: 例：``http://home.banstack.top:8092``。**不带尾斜杠**。
    offline_public_base: str = ""

    #: 按用户的请求数限流（每分钟）。0 = 不限流。
    #:
    #: 单个账号不能无限拉 —— 多个账号共用同一份媒体库，一个账号狂刷
    #: 会把磁盘 IO 和带宽吃满。
    #:
    #: 比 IP 维度宽（2400/min）：一个账号可能在多台设备上用（电视 + 手机），
    #: 但单个账号的合理用量仍远低于 IP 总量。
    dav_user_rate_limit_per_min: int = Field(default=2400, ge=0)

    #: 认证失败多少次后锁定（按 IP 与按用户名**各自独立**计数）。
    dav_max_failures: int = Field(default=5, ge=1)

    #: 锁定时长（秒）。
    dav_lock_sec: int = Field(default=900, ge=0)

    #: WebDAV 认证结果缓存时长（秒）。
    #:
    #: **性能必需**：PBKDF2 600k 迭代实测约 42 ms，Infuse 扫一次库要发
    #: 几百个 PROPFIND —— 不缓存就是几十秒起。
    #:
    #: 安全边界：缓存 key 含 IP 与 Authorization 头哈希，且**该 IP 一旦
    #: 认证失败立即清空其全部缓存**。
    dav_auth_cache_sec: float = Field(default=60.0, ge=0.0)

    #: 审计日志留存天数。0 = 永久保留。
    dav_audit_retention_days: int = Field(default=90, ge=0)

    #: 审计日志里 User-Agent 的最大长度（截断存储）。
    dav_audit_client_max_len: int = Field(default=255, ge=32)

    #: WebDAV 允许的请求体大小上限（字节）。只读 DAV 用不到 body。
    dav_max_body_bytes: int = Field(default=1024 * 1024, ge=0)

    #: wsgidav 的 WSGI 线程池大小。
    #:
    #: DAV 请求在线程池里跑（wsgidav 是同步 WSGI 应用），与主 event loop
    #: 分离。太小会排队，太大会占内存。
    dav_wsgi_workers: int = Field(default=8, ge=1, le=64)

    # -- 上传任务（本地目录 → 光鸭盘）--
    #: 监听时是否用轮询模式（不依赖 inotify）。
    #:
    #: **默认 True**：生产路径常常是 CIFS / FUSE 挂载（Unraid 的
    #: ``/mnt/user`` 是 shfs），**都不支持 inotify**——用 inotify 会
    #: 「监听不到任何变化」，看起来像任务坏了。轮询慢一点但能用。
    #: 纯本地 ext4 / APFS 可设为 false 换更低的 CPU 占用。
    upload_watch_polling: bool = True

    #: 轮询模式的检查间隔（毫秒）
    upload_watch_poll_ms: int = Field(default=1000, ge=100, le=60000)

    #: 事件防抖窗口（毫秒）——把一次「写文件」引发的多条事件合并成一条
    upload_watch_debounce_ms: int = Field(default=1500, ge=0, le=60000)

    #: 文件「写完」判定：两次采样之间 size/mtime 不变才算稳态。
    #: 间隔（秒）——太小会误判正在写入的文件，太大会拖慢响应。
    upload_stable_check_sec: float = Field(default=1.5, ge=0.1, le=60.0)

    #: 覆盖前删除旧文件后，等「同名已消失」的重试次数与间隔（秒）
    upload_delete_verify_retries: int = Field(default=10, ge=1, le=100)
    upload_delete_verify_delay_sec: float = Field(default=0.5, ge=0.05, le=30.0)

    #: 单个上传任务的队列上限（待传文件数）。超出后进溢出暂存（不丢），
    #: 暂存也满才真丢并计数
    upload_queue_maxsize: int = Field(default=1000, ge=1, le=100000)

    #: 小/大文件**通道分流**阈值（字节）：size < 阈值 → 小文件通道
    #: （可并发，K = 任务 concurrency 列）；≥ 阈值 → 大文件通道（恒串行）。
    #: 0 = 全部走大文件通道。
    #:
    #: ⚠️ 与 ``guangya_upload.SMALL_FILE_THRESHOLD``（单 PUT vs 分片）是
    #: **两回事**——这里是流水线分流语义，默认值对齐 1MB（实测 <1MB 单 PUT
    #: 是唯一支持并发的上传路径，见 ``docs/upload-bandwidth.md``）。
    upload_small_file_threshold: int = Field(
        default=1024 * 1024, ge=0, le=1024 * 1024 * 1024
    )

    # -- 优雅停机（FR-8.2）--
    shutdown_grace_seconds: int = 60

    @field_validator("web_username")
    @classmethod
    def _warn_default_admin(cls, v: str) -> str:
        """安全提示：默认 admin 用户名需在生产环境修改；不阻塞启动，仅打日志。"""
        import logging

        if v == "admin":
            logging.getLogger(__name__).warning(
                "WEB_USERNAME 使用默认值 'admin'，建议在生产环境改为唯一值"
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"LOG_LEVEL 取值非法：{v}")
        return level

    @property
    def db_file(self) -> Path:
        return self.db_path or (self.data_dir / "medialab.db")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_file}"

    @property
    def resolved_public_base_url(self) -> str:
        """对外基础地址（去掉尾部斜杠）——写进 STRM 文件的 URL 用它。

        刻意**不提供**「自动探测本机 IP」的魔法——猜错会让全部 STRM 失效。
        未配置时返回空串，调用方（STRM 生成）应拒绝生成并提示用户配置。
        """
        return self.public_base_url.rstrip("/")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def _get_settings_cached() -> AppSettings:
    """从环境变量 / ``.env`` 读取的进程内单例。"""
    return AppSettings()


#: 全局 settings 覆盖（测试 / 嵌入场景注入）。
#:
#: 为 None 时 :func:`get_settings` 走环境变量路径（生产行为）；
#: 非 None 时一律返回该对象，忽略环境变量。
_settings_override: AppSettings | None = None


def set_settings(settings: AppSettings | None) -> None:
    """覆盖全局 settings（测试 / 嵌入场景）。

    传 ``None`` 恢复为「从环境变量读取」。

    历史：原先 :func:`create_app` 用 ``get_settings.cache_clear()`` 试图
    注入 settings，但 :func:`get_settings` 是**无参**的 lru_cache 工厂——
    ``cache_clear()`` 只是让它下次重读 ``.env``，传入的 settings 对象被
    完全丢弃，注入形同虚设（测试无法用独立 DB，见 Phase 0 计划）。
    本函数提供真正的注入通道。
    """
    global _settings_override
    _settings_override = settings


def get_settings() -> AppSettings:
    """进程内单例。有 override 时返回 override，否则读环境变量。"""
    if _settings_override is not None:
        return _settings_override
    return _get_settings_cached()