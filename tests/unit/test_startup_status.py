"""启动降级台账的单元行为。"""

from __future__ import annotations

from app.services.startup_status import clear, record, reset, snapshot


def test_record_overwrites_same_component() -> None:
    """同一组件重复登记覆盖旧记录（当前态，不累加历史）。"""
    reset()
    record("watch_manager_start:foo", "第一次失败")
    record("watch_manager_start:foo", "第二次失败")
    snap = snapshot()
    assert len(snap) == 1
    assert snap["watch_manager_start:foo"]["reason"] == "第二次失败"
    assert snap["watch_manager_start:foo"]["at"]
    reset()


def test_clear_and_reset() -> None:
    reset()
    record("a", "x")
    record("b", "y")
    clear("a")
    assert list(snapshot()) == ["b"]
    reset()
    assert snapshot() == {}


def test_reason_is_truncated() -> None:
    """异常信息超长时截断，避免台账变成日志转储。"""
    reset()
    record("boom", "x" * 2000)
    reason = snapshot()["boom"]["reason"]
    assert len(reason) < 600
    assert reason.endswith("…")
    reset()
