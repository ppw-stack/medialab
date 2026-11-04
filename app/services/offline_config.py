"""离线下载的**配置读取与保存**（DB 持久化，改完立即生效）。

配置项落在 ``app_env_settings`` 表（与「辅种扫描目录」同一套机制），
所以**用户在页面上改完立即生效**，且跨重启保持——不用改 ``.env``、
不用重启容器。

⚠️ **文件服务的路由必须在启动时挂载**（FastAPI 不支持运行时增删路由），
故 :func:`app.api.routes.offline_files.mount_offline_files` **无条件挂载**，
由**每次请求时**读本模块的配置来决定放行还是拒绝。

后果：未启用时 ``/offline-files/...`` 返回 **403**（而不是 SPA 的
index.html）——这更诚实（明确「这里有个服务但没开」，而不是「页面不存在」）。

**分层**：DB 访问（``session_scope``）在本模块，API 层只调
:func:`apply_offline_config`——``app/api/`` 不得直接碰 DB
（``tests/unit/test_layering.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 配置键（与 ``AppSettings`` 字段名一致，便于 env 兜底）
KEY_SECRET = "offline_hmac_secret"
KEY_PUBLIC_BASE = "offline_public_base"
KEY_FILES_ROOT = "offline_files_root"


@dataclass(frozen=True, slots=True)
class OfflineConfig:
    """离线下载的当前配置。"""

    #: HMAC 密钥（空 = 未启用）
    secret: str
    #: 公网入口（空 = 未启用）
    public_base: str
    #: 源根目录（空 = 未启用）
    files_root: str

    @property
    def enabled(self) -> bool:
        """三项齐全才算启用。"""
        return bool(self.secret and self.public_base and self.files_root)


def _read(key: str) -> str:
    """读一项配置：**DB 优先**，env（``AppSettings``）兜底。

    DB 未初始化时（测试 / 嵌入式场景）回落到 env 来源值。
    """
    try:
        from app.services.env_settings import get_env_setting

        return get_env_setting(key)
    except (RuntimeError, KeyError):
        # manager 未初始化或 key 未注册 → 用 env 来源
        return str(getattr(get_settings(), key, "") or "")


def current_config() -> OfflineConfig:
    """取当前配置。"""
    return OfflineConfig(
        secret=_read(KEY_SECRET),
        public_base=_read(KEY_PUBLIC_BASE),
        files_root=_read(KEY_FILES_ROOT),
    )


class OfflineConfigError(ValueError):
    """配置非法（公网入口不是 URL / 源根目录不存在）。"""


def normalize_public_base(value: str) -> str:
    """规范化公网入口：去空白 + 去尾斜杠。

    尾斜杠必须去掉——生成 URL 时是 ``f"{base}{URL_PREFIX}/..."``，
    留着会得到 ``//offline-files``，部分服务器/代理会当不同路径处理。
    """
    return value.strip().rstrip("/")


def validate_config(updates: dict[str, str]) -> None:
    """校验配置值。失败抛 :class:`OfflineConfigError`。

    只做**这里特有**的语义校验（URL 形状 / 目录存在）；通用校验
    （长度 / 已注册）由 :func:`app.services.env_settings.validate` 负责。
    """
    base = updates.get(KEY_PUBLIC_BASE, "")
    if base and not base.startswith(("http://", "https://")):
        raise OfflineConfigError("公网入口必须以 http:// 或 https:// 开头")

    root = updates.get(KEY_FILES_ROOT, "")
    if root and not Path(root).is_dir():
        raise OfflineConfigError(f"源根目录不存在：{root}")


async def apply_offline_config(updates: dict[str, str]) -> None:
    """保存配置：校验 → **DB 先 → 内存后**。

    事务提交失败时内存未变，作业不会拿到一个重启后就消失的值
    （与 :func:`app.services.runtime_params.apply_runtime_params` 同一处理）。

    Args:
        updates: 只含要改的键（值已由调用方 ``strip`` 过）。

    Raises:
        OfflineConfigError: 配置非法。
        RuntimeError: DB 写入失败。
    """
    from app.db.session import session_scope
    from app.services.env_settings import get_env_settings, validate

    if not updates:
        raise OfflineConfigError("未提供任何要修改的配置")

    validate_config(updates)
    try:
        validate(updates)
    except ValueError as exc:
        raise OfflineConfigError(str(exc)) from exc

    mgr = get_env_settings()
    async with session_scope() as session:
        await mgr.apply(session, updates)
    for key, value in updates.items():
        mgr.set_memory(key, value)
    logger.info("离线下载配置已更新：%s", sorted(updates))


__all__ = [
    "KEY_FILES_ROOT",
    "KEY_PUBLIC_BASE",
    "KEY_SECRET",
    "OfflineConfig",
    "OfflineConfigError",
    "apply_offline_config",
    "current_config",
    "normalize_public_base",
    "validate_config",
]
