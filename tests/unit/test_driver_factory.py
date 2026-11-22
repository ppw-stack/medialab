"""驱动工厂：``on_token_refresh`` 的透传与不吞真实错误。

**回归背景**（2026-11-22 评审）：旧实现用 ``try/except TypeError`` 判断
「工厂是否接受 on_token_refresh」——构造函数**内部**抛出的 TypeError
（参数拼错、类型不对）也会被吞掉，然后静默退化成不带回调的重建：
真实缺陷被掩盖，驱动还被初始化两次。

改成**签名判断**后，两条边界各有一个用例：
- 接受回调的工厂 → 收到回调
- 不接受的工厂 → 不收回调，且**内部 TypeError 照常冒泡**
"""

from __future__ import annotations

from typing import Any

import pytest
from app.adapters.drivers.base import DriverError
from app.services import driver_factory
from app.services.driver_factory import build_driver


def test_接受回调的工厂收到回调(monkeypatch: pytest.MonkeyPatch) -> None:
    """签名里有 ``on_token_refresh`` → 必须透传（漏传会让刷新后不写回 DB）。"""
    captured: dict[str, Any] = {}

    class _Drv:
        name = "fake"

    def _factory(credential: str | None, *, on_token_refresh: Any = None) -> Any:
        captured["credential"] = credential
        captured["cb"] = on_token_refresh
        return _Drv()

    monkeypatch.setitem(driver_factory._REGISTRY, "fake", (_factory, lambda c: c))

    def _cb(_cred: Any) -> None:  # pragma: no cover - 只验身份
        return None

    build_driver("fake", "cred", on_token_refresh=_cb)
    assert captured["credential"] == "cred"
    assert captured["cb"] is _cb


def test_不接受回调的工厂按普通构造(monkeypatch: pytest.MonkeyPatch) -> None:
    """签名里没有该参数 → 普通构造，不传关键字。"""
    captured: dict[str, Any] = {}

    class _Drv:
        name = "fake"

    def _factory(credential: str | None) -> Any:
        captured["credential"] = credential
        return _Drv()

    monkeypatch.setitem(driver_factory._REGISTRY, "fake", (_factory, lambda c: c))

    driver = build_driver("fake", "cred", on_token_refresh=lambda c: None)
    assert isinstance(driver, _Drv)
    assert captured["credential"] == "cred"


def test_工厂内部抛出的TypeError不被吞掉(monkeypatch: pytest.MonkeyPatch) -> None:
    """构造函数内部的 TypeError 必须冒泡——那是真 bug，不是「不支持回调」。

    旧实现会吞掉它并**再构造一次**，缺陷被掩盖。
    """
    calls: list[int] = []

    def _factory(credential: str | None, *, on_token_refresh: Any = None) -> Any:
        calls.append(1)
        raise TypeError("构造函数内部的真实类型错误")

    monkeypatch.setitem(driver_factory._REGISTRY, "fake", (_factory, lambda c: c))

    with pytest.raises(TypeError, match="真实类型错误"):
        build_driver("fake", "cred", on_token_refresh=lambda c: None)
    assert len(calls) == 1, "不该重试第二次构造"


def test_未注册类型抛DriverError() -> None:
    with pytest.raises(DriverError) as exc:
        build_driver("nope-not-registered")
    assert exc.value.kind == "not_found"
