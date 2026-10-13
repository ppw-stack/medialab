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

from app.core.crypto import CredentialCipher

#: 密钥生成提示，避免用户猜测格式
KEY_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'


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
    # -- 前端静态资源路径（FR-6.1 SPA 托管）--
    frontend_dist: Path = Path("/app/web/dist")

    # -- 凭据加密（FR-7.1）--
    encryption_key: SecretStr = Field(
        ...,
        description="32 字节 URL-safe base64；缺失则启动失败，禁止明文降级",
    )
    key_version: int = 1

    # -- 数据库（§8 SQLite 约定）--
    db_path: Path | None = Field(default=None, description="默认 data_dir/seedhelper.db")
    db_journal_mode: Literal["WAL", "DELETE"] = "WAL"
    db_synchronous: Literal["NORMAL", "FULL", "OFF"] = "NORMAL"
    db_single_writer_queue: bool = True

    # -- 调度 --
    scheduler_enabled: bool = True
    #: 统一 RSS 拉取 cron 表达式（5 字段：分 时 日 月 星期），默认每 10 分钟
    fetch_all_sites_cron: str = "*/10 * * * *"
    #: 自动推送免费种到默认下载器的 cron（默认 30 分钟一次，与 fetch 错开）
    push_free_cron: str = "*/30 * * * *"
    #: 免费窗口巡检 cron（5/15/25/... 错峰 fetch 0/10/20/30 与 push 0/30）
    monitor_free_window_cron: str = "5,15,25,35,45,55 * * * *"
    #: 距 free_end_at 不足此分钟数且未完成 → 自动暂停下载器任务
    monitor_pre_pause_minutes: int = 10
    #: 限时种剩余时间低于此值不推（小时，默认 24）
    push_min_remaining_hours: int = 24
    #: 每次推送最多 N 个（避免下载器瞬时过载）
    push_max_per_run: int = 20

    # -- 优雅停机（FR-8.2）--
    shutdown_grace_seconds: int = 60

    @field_validator("encryption_key")
    @classmethod
    def _validate_key(cls, v: SecretStr) -> SecretStr:
        """fail-fast：密钥必须是 32 字节 base64（FR-7.1）。

        接受 ``secrets.token_urlsafe(32)`` 产出的无填充 URL-safe base64——
        这是文档与 ``.env.example`` 推荐的生成方式。
        """
        raw = v.get_secret_value().strip()
        if not raw:
            raise ValueError("ENCRYPTION_KEY 不能为空；生成命令：" + KEY_HINT)
        try:
            CredentialCipher(raw)
        except ValueError as exc:
            raise ValueError(f"{exc}；生成命令：{KEY_HINT}") from exc
        return v

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
    """进程内单例。首次调用即触发 ENCRYPTION_KEY 校验，失败则进程启动中止。"""
    return AppSettings()