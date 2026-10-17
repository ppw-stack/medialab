"""配置中心（FR-7）。

配置来源优先级：环境变量 > 数据库 > 配置文件默认值。

本模块只负责 **进程级启动配置**（环境/文件来源），站点与下载器的业务配置
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
    config_file: Path | None = Field(
        default=None, description="可选的 YAML 配置文件（最低优先级）"
    )

    # -- Web 面板（FR-6 / 非功能·安全性）--
    web_host: str = "0.0.0.0"
    web_port: int = 8080
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
    db_single_writer_queue: bool = True

    # -- 调度 --
    scheduler_enabled: bool = True
    #: 站点拉取 cron 表达式（5 字段：分 时 日 月 星期），默认每 10 分钟
    fetch_all_sites_cron: str = "*/10 * * * *"
    #: 自动推送免费种到默认下载器的 cron（默认 30 分钟一次，与 fetch 错开）
    push_free_cron: str = "*/30 * * * *"
    #: 免费窗口巡检 cron（5/15/25/... 错峰 fetch 0/10/20/30 与 push 0/30）
    monitor_free_window_cron: str = "5,15,25,35,45,55 * * * *"
    #: 距 free_end_at 不足此分钟数且未完成 → 自动暂停下载器任务
    monitor_pre_pause_minutes: int = 10
    #: 限时种剩余时间低于此值不推（小时，默认 6）
    push_min_remaining_hours: int = 6
    #: 每次推送最多 N 个（避免下载器瞬时过载）
    push_max_per_run: int = 20
    #: 连续下载 .torrent 文件的间隔（毫秒，默认 2000 = 2 秒），避免触发站点限频
    push_download_interval_ms: int = 2000

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
def get_settings() -> AppSettings:
    """进程内单例。"""
    return AppSettings()