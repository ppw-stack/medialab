"""系统配置的只读适配（移植 spider 的依赖）。

被移植的 spider 只构造 ``systemconfig`` 并可能在个别站读取少量键；
本项目没有对应配置项，统一返回 ``None``，由各 spider 的默认值兜底。
"""

from __future__ import annotations

from typing import Any


class _SystemConfig:
    def get(self, _key: str, default: Any = None) -> Any:
        return default


def get_configured_system_config() -> _SystemConfig:
    """返回一个只读的配置对象。"""
    return _SystemConfig()
