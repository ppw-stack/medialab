"""免费种合并作业（推送 + 窗口监测）的编排契约（2026-11-22 合并）。

合并的**唯一目的**是让两件事共用一条时间轴：监测每 10 分钟跑一次（临期
暂停是安全动作），推送被内部节流到 ≥30 分钟一次（站点有限频）。

所以本文件锁的是「编排语义」，不是两个子服务自己的业务规则
（那些在 ``test_push_free`` / ``test_monitor`` 里）：

1. 监测**每轮都跑**，不受推送节流影响；
2. 推送在门槛内**跳过**，且跳过**不刷新**节流时间戳；
3. 空转（``selected == 0``）**不刷新**时间戳——否则用户刚配好下载器
   也要白等 30 分钟；
4. 一段失败**不影响**另一段（各自容错，失败进 steps）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from app.services import push_and_monitor
from app.services.scheduler import JobStep

# ------------------------------------------------------------------ 假服务


@dataclass
class _FakePushResult:
    selected: int = 1
    pushed: int = 1
    failed: int = 0
    errors: tuple[str, ...] = ()
    summary: str = "已推 1"
    steps: tuple[JobStep, ...] = field(default_factory=tuple)


class _FakePushService:
    """替身：记录调用次数，返回可控结果。"""

    calls = 0
    result = _FakePushResult()
    raises: Exception | None = None

    def __init__(self, _factory: Any) -> None:
        pass

    async def run(self) -> _FakePushResult:
        type(self).calls += 1
        if type(self).raises is not None:
            raise type(self).raises
        return type(self).result


@dataclass
class _FakeMonitorResult:
    scanned: int = 3
    matched: int = 1
    summary: str = "命中 1"
    steps: tuple[JobStep, ...] = field(default_factory=tuple)


class _FakeMonitorService:
    calls = 0
    raises: Exception | None = None

    def __init__(self, _factory: Any) -> None:
        pass

    async def run(self) -> _FakeMonitorResult:
        type(self).calls += 1
        if type(self).raises is not None:
            raise type(self).raises
        return _FakeMonitorResult()


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换两个子服务 + 复位节流状态（进程内全局变量）。"""
    import app.services.monitor as monitor_mod
    import app.services.push_free as push_mod

    monkeypatch.setattr(push_mod, "PushFreeService", _FakePushService)
    monkeypatch.setattr(monitor_mod, "MonitorFreeWindowService", _FakeMonitorService)
    _FakePushService.calls = 0
    _FakePushService.result = _FakePushResult()
    _FakePushService.raises = None
    _FakeMonitorService.calls = 0
    _FakeMonitorService.raises = None
    push_and_monitor.reset_throttle_state()


def _stage(result: dict[str, Any], name: str) -> JobStep:
    for step in result["steps"]:
        if step.name == name:
            return step
    raise AssertionError(f"缺步骤：{name}")


# -------------------------------------------------------------------- 编排


async def test_first_run_runs_both_parts() -> None:
    """首轮（本进程还没推过）：监测 + 推送都跑。"""
    out = await push_and_monitor.run_push_and_monitor(None)

    assert _FakeMonitorService.calls == 1
    assert _FakePushService.calls == 1
    assert _stage(out, "免费窗口监测").status == "ok"
    assert _stage(out, "免费种推送").status == "ok"


async def test_push_skipped_within_interval() -> None:
    """门槛内（30 分钟）不再推，但**监测照旧跑**。

    这是合并的关键：监测的节奏（10 分钟）不能被推送的节流拖慢。
    """
    await push_and_monitor.run_push_and_monitor(None)
    assert _FakePushService.calls == 1

    out = await push_and_monitor.run_push_and_monitor(None)
    assert _FakeMonitorService.calls == 2, "监测必须每轮都跑"
    assert _FakePushService.calls == 1, "推送应被门槛挡住"
    step = _stage(out, "免费种推送")
    assert step.status == "skip"
    assert "门槛" in (step.detail or "")


async def test_idle_run_does_not_refresh_throttle() -> None:
    """空转（无候选）不刷新时间戳：下一轮可以立刻再试。

    否则用户刚配好默认下载器 / 刚有新候选，还要白等 30 分钟。
    """
    _FakePushService.result = _FakePushResult(
        selected=0, pushed=0, summary="无新候选"
    )
    await push_and_monitor.run_push_and_monitor(None)
    assert _FakePushService.calls == 1

    # 第二轮：若空转刷了时间戳，这里会被挡住（calls 仍是 1）
    _FakePushService.result = _FakePushResult(selected=1, pushed=1, summary="已推 1")
    await push_and_monitor.run_push_and_monitor(None)
    assert _FakePushService.calls == 2


async def test_monitor_failure_does_not_block_push() -> None:
    """监测段抛错 → 推送段照跑（各自容错）。"""
    _FakeMonitorService.raises = RuntimeError("downloader down")

    out = await push_and_monitor.run_push_and_monitor(None)

    assert _stage(out, "免费窗口监测").status == "fail"
    assert _FakePushService.calls == 1
    assert _stage(out, "免费种推送").status == "ok"


async def test_push_failure_does_not_lose_summary() -> None:
    """推送段抛错 → 监测结果仍在 summary 里（用户看得见前半段成效）。"""
    _FakePushService.raises = RuntimeError("boom")

    out = await push_and_monitor.run_push_and_monitor(None)

    assert _stage(out, "免费种推送").status == "fail"
    assert "命中 1" in out["summary"]


async def test_summary_deduped_when_both_parts_say_same_thing() -> None:
    """两段返回同一句（都没配下载器）时，summary 不能重复拼两遍。"""
    _FakePushService.result = _FakePushResult(
        selected=0, pushed=0, summary="未配置默认下载器"
    )
    _FakeMonitorService.run = _monitor_without_downloader  # type: ignore[assignment]

    out = await push_and_monitor.run_push_and_monitor(None)
    assert out["summary"] == "未配置默认下载器"


async def _monitor_without_downloader(self: Any) -> _FakeMonitorResult:
    return _FakeMonitorResult(scanned=0, matched=0, summary="未配置默认下载器")


async def test_children_carry_sub_steps() -> None:
    """子服务自带的步骤挂成 children（前端步骤卡片能展开看细节）。"""
    _FakeMonitorService.run = _monitor_with_steps  # type: ignore[assignment]

    out = await push_and_monitor.run_push_and_monitor(None)
    step = _stage(out, "免费窗口监测")
    assert step.children is not None
    assert step.children[0].name == "扫描已推种子"


async def _monitor_with_steps(self: Any) -> _FakeMonitorResult:
    return _FakeMonitorResult(
        steps=(JobStep("扫描已推种子", "ok", 3, "3 个"),),
    )
