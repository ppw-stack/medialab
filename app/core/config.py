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

    # -- 优雅停机（FR-8.2）--
    shutdown_grace_seconds: int = 60

    @field_validator("encryption_key")
    @classmethod
    def _validate_key(cls, v: SecretStr) -> SecretStr:
        """fail-fast：密钥必须是 32 字节 URL-safe base64（FR-7.1）。"""
        raw = v.get_secret_value().strip()
        if not raw:
            raise ValueError("ENCRYPTION_KEY 不能为空；生成命令：" + KEY_HINT)
        import base64

        try:
            decoded = base64.urlsafe_b64decode(raw.encode())
        except Exception as exc:
            raise ValueError(
                f"ENCRYPTION_KEY 不是合法的 URL-safe base64；生成命令：{KEY_HINT}"
            ) from exc
        if len(decoded) != 32:
            raise ValueError(
                f"ENCRYPTION_KEY 解码后应为 32 字节，实际 {len(decoded)} 字节；"
                f"生成命令：{KEY_HINT}"
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