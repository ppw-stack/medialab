"""启动降级台账：让 ``/readyz`` 看见「进程活着但功能缺失」。

启动链路里有若干「失败只记日志、继续启动」的降级路径（监听器加载失败、
驱动测连失败、密码升级失败……）。它们不会阻断进程，容器探针看到的是
``healthy``，运维完全无感——直到用户报告某个任务不工作。

本模块提供一个进程级台账：降级发生时 :func:`record`，``/readyz`` 用
:func:`snapshot` 读出明细并把 ``status`` 标为 ``degraded``。

语义是**当前态**：每个 component 只保留一条（覆盖而非累加），成功恢复
时可用 :func:`clear` 清除；每次 lifespan 开始 :func:`reset` 清空，避免
测试里多次 ``create_app`` 互相串。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from app.core.timeutils import bj_now

#: 单条原因的最大长度——异常信息可能很长，不让台账变成日志转储。
_MAX_REASON_LEN = 500


@dataclass(frozen=True)
class Degradation:
    component: str
    reason: str
    at: str

    def to_dict(self) -> dict[str, str]:
        return {"component": self.component, "reason": self.reason, "at": self.at}


_LEDGER: dict[str, Degradation] = {}
_LOCK = threading.Lock()


def record(component: str, reason: object) -> None:
    """登记一个降级（同 component 重复登记会覆盖旧记录）。

    ``reason`` 可以是异常对象或字符串，统一存成截断后的字符串。
    """
    text = str(reason)
    if len(text) > _MAX_REASON_LEN:
        text = text[:_MAX_REASON_LEN] + "…"
    with _LOCK:
        _LEDGER[component] = Degradation(
            component=component, reason=text, at=str(bj_now())
        )


def clear(component: str) -> None:
    """清除某个降级（该组件恢复正常时调用）。"""
    with _LOCK:
        _LEDGER.pop(component, None)


def snapshot() -> dict[str, dict[str, str]]:
    """返回当前降级的只读快照（按 component 排序）。"""
    with _LOCK:
        return {key: _LEDGER[key].to_dict() for key in sorted(_LEDGER)}


def reset() -> None:
    """清空台账（lifespan 入口调用）。"""
    with _LOCK:
        _LEDGER.clear()


__all__ = ["Degradation", "clear", "record", "reset", "snapshot"]
