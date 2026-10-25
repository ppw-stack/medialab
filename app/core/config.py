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
    db_path: Path | None = Field(default=None, description="默认 data_dir/seedhelper.db")
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
        return self.db_path or (self.data_dir / "seedhelper.db")

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_file}"

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