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
    #: 站点拉取 cron 表达式（5 字段：分 时 日 月 星期），默认每 10 分钟
    fetch_all_sites_cron: str = "*/10 * * * *"
    #: 自动推送免费种到默认下载器的 cron（默认 30 分钟一次，与 fetch 错开）
    push_free_cron: str = "*/30 * * * *"
    #: 免费窗口巡检 cron（5/15/25/... 错峰 fetch 0/10/20/30 与 push 0/30）
    monitor_free_window_cron: str = "5,15,25,35,45,55 * * * *"
    #: 辅种下载器巡查 cron (每小时 0/6/12/18 分, 错峰不与 fetch/push/monitor 冲突)
    scan_cross_seed_cron: str = "0 */6 * * *"
    #: 站点每日签到 cron（默认每天 08:37）。
    #: 分钟位 37 刻意避开既有多 5 分钟的作业（fetch */10 / push */30 /
    #: monitor 5,15,25... / drain */5 / scan 0 */6），避免同一分钟并发抢资源。
    daily_checkin_cron: str = "37 8 * * *"

    # -- AI 读码（FR-3.6 站点签到的图形验证码）--
    # 三项**全为空则视为未配置**，需人工识别的站点会降级为 UNSUPPORTED
    # 而不是让作业变红。走 OpenAI 兼容协议（/v1/chat/completions）。
    ai_api_base: str = ""
    ai_api_key: SecretStr | None = None
    #: 必须是**视觉**模型（能吃 image_url 内容块）。纯文本模型读不了验证码。
    ai_model: str = ""
    ai_timeout_seconds: float = 60.0
    #: 距 free_end_at 不足此分钟数且未完成 → 自动暂停下载器任务
    monitor_pre_pause_minutes: int = 10
    #: 限时种剩余时间低于此值不推（小时，默认 6）
    push_min_remaining_hours: int = 6
    #: 每次推送最多 N 个（避免下载器瞬时过载）
    push_max_per_run: int = 20
    #: 连续下载 .torrent 文件的间隔（毫秒，默认 2000 = 2 秒），避免触发站点限频
    push_download_interval_ms: int = 2000
    #: push_free 选种时「同档内」的排序方式（0=按时魔进度升序 1=随机）
    #:
    #: 档位（未封顶 / 无信号 / 已封顶）**始终**是主排序；本项只决定同一档内
    #: 候选怎么排。0 = 按各站时魔进度升序（默认，进度低的站先）；
    #: 1 = 整个档内随机打乱（含站点顺序）。
    push_within_bucket_order: int = 0

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