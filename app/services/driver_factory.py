"""驱动工厂。

按 ``driver.type`` 构造对应驱动实例。当前只注册 ``guangya``。

新增驱动只需：实现 :class:`app.adapters.drivers.base.CloudDriver` 协议，
在此注册表中加一行——中转层与缓存层不用改。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from app.adapters.drivers.base import CloudDriver, DriverError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 驱动构造函数注册表：type → (构造函数, 凭据解码器)
#:
#: 构造函数签名：``(credential: str | None) -> CloudDriver``
#: 凭据解码器负责把 DB 里的密文还原成驱动能用的凭据（各驱动凭据格式不同）。
#:
#: 构造函数用 ``Callable[..., CloudDriver]``——部分驱动（光鸭）还接受
#: ``on_token_refresh`` 回调；不支持的会在调用时抛 TypeError 并退回普通构造。
DriverFactory = Callable[..., CloudDriver]
CredentialDecoder = Callable[[str | None], str | None]

_REGISTRY: dict[str, tuple[DriverFactory, CredentialDecoder]] = {}


def register_driver(
    driver_type: str,
    factory: DriverFactory,
    credential_decoder: CredentialDecoder | None = None,
) -> None:
    """注册一个驱动类型。"""

    def _passthrough(credential: str | None) -> str | None:
        return credential

    _REGISTRY[driver_type] = (factory, credential_decoder or _passthrough)


def build_driver(
    driver_type: str,
    credential: str | None = None,
    *,
    on_token_refresh: Callable[[Any], Any] | None = None,
) -> CloudDriver:
    """按类型构造驱动实例。

    Args:
        driver_type: 驱动类型（``guangya``）。
        credential: 加密后的凭据。
        on_token_refresh: Token 刷新后的回调（光鸭用——它的 access_token
            只有 1~2 小时有效期，刷新后**必须**写回 DB，否则重启后又用
            旧 token 导致 401）。回调参数为驱动自己的凭据对象。

    Raises:
        DriverError: 类型未注册（错误分类 ``not_found``）。
    """
    entry = _REGISTRY.get(driver_type)
    if entry is None:
        raise DriverError(
            f"未注册的驱动类型：{driver_type}",
            kind="not_found",
        )
    factory, decoder = entry
    plain = decoder(credential)
    if on_token_refresh is not None:
        # 用**签名判断**而不是 try/except TypeError：构造函数内部抛出的
        # TypeError（参数拼错、类型不对）会被 except 吞掉，然后静默退化成
        # 不带回调的重建——真实缺陷被掩盖，且驱动被初始化两次
        # （2026-11-22 评审指出）。
        if _accepts_token_refresh(factory):
            return factory(plain, on_token_refresh=on_token_refresh)
        logger.debug(
            "驱动 %s 不接受 on_token_refresh，按普通构造（刷新后的 token 不会写回 DB）",
            driver_type,
        )
    return factory(plain)


def _accepts_token_refresh(factory: DriverFactory) -> bool:
    """构造函数是否接受 ``on_token_refresh`` 关键字参数。

    取不到签名（C 扩展 / ``functools.partial`` 等）时按**接受**处理——
    让驱动自己决定，而不是静默丢掉回调（丢回调会让 Token 刷新后不写回）。
    """
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - 无法取签名的构造器
        return True
    return "on_token_refresh" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def registered_types() -> list[str]:
    """已注册的驱动类型列表。"""
    return sorted(_REGISTRY)


def _register_builtin() -> None:
    """注册内置驱动。

    ``guangya`` 依赖 guangyaclient（可选 extra）。此处用延迟导入，
    缺失依赖时只在**实际构造该驱动**时报错，不影响其它功能启动。
    """
    try:
        from app.adapters.drivers.guangya import build_guangya_driver
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的降级路径
        logger.debug("光鸭驱动不可用（依赖未安装）：%s", exc)
    else:
        # 光鸭的凭据是一组字段（token 三件套），由驱动**自己**解密——
        # 解码器的契约是「返回字符串」，塞不下一组结构化数据。
        register_driver("guangya", build_guangya_driver)


_register_builtin()
