"""运行时守卫：事件循环心跳看门狗 + 外部挂载探活。

**背景（2026-11-06 prod 事故）**：CIFS 挂载卡死（``hard`` 模式无限等待）
→ 事件循环里的一次**同步** ``stat`` 把整个 loop 卡住 → uvicorn 无法处理
请求 → ``/healthz`` 连接被 RST → 容器 unhealthy、**面板实际不可用**。
挂载恢复后 loop 自动解开（实测约 20 分钟），但**现场无法自动取证**——
当晚是手动装 py-spy 才抓到卡点。

本模块提供两个后台线程，**都绝不阻塞事件循环**：

1. **心跳看门狗**：每 5 秒用 ``loop.call_soon_threadsafe`` 投递一次心跳，
   回调里更新时间戳。loop 被卡住时心跳滞后 → ``loop_stalled_ms`` 暴露在
   ``/healthz``；滞后超阈值时自动 **py-spy dump** 落日志（把「卡在哪一行」
   留下来），dump 本身在另一个线程里跑，不挡看门狗。
2. **挂载探活**：每 60 秒在**独立线程**里列一次挂载根目录——CIFS 卡死时
   这个线程会卡住（无害），缓存里的 ``checked_at`` 变陈旧 →
   ``/healthz`` 的 ``mounts`` 字段显示 ``hang``。

设计约束：

- ``snapshot()`` **只读缓存**，绝不做 IO——它会被 ``/healthz`` 同步调用
- 探活线程卡死后**不重复起线程**（每个路径同时只允许一个在飞），
  避免挂死时线程堆积
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.core.timeutils import bj_now

logger = get_logger(__name__)

#: 心跳间隔（秒）——loop 滞后超过 ~2 个间隔即视为卡住
HEARTBEAT_INTERVAL_SEC = 5.0
#: loop 滞后多少秒算「卡住」（触发自动取证）
STALL_THRESHOLD_SEC = 10.0
#: 挂载探活间隔（秒）
PROBE_INTERVAL_SEC = 60.0
#: py-spy 的候选路径（容器里手动装的版本不在 PATH 上）
PYSPY_CANDIDATES = (
    "py-spy",
    "/srv/medialab/.local/bin/py-spy",
    "/usr/local/bin/py-spy",
)


@dataclass
class MountProbe:
    """单个挂载点的探活状态。"""

    path: str
    #: ``ok`` / ``hang`` / ``error`` / ``unknown``
    status: str = "unknown"
    #: 最近一次**完成**探活的时间（北京时间）
    checked_at: str | None = None
    #: 失败原因（error 时）
    error: str | None = None
    #: 正在飞行的探活线程（卡死后不再重复起线程）
    _inflight: bool = False


@dataclass
class _GuardState:
    """看门狗状态（全部为「非阻塞读」的缓存）。"""

    loop: Any = None
    #: 最近一次心跳落地的时间（monotonic）
    last_beat: float = field(default_factory=time.monotonic)
    #: 已对当前这次卡顿取证过（避免每轮都 dump）
    dumped_for_stall: bool = False
    mounts: dict[str, MountProbe] = field(default_factory=dict)
    threads: list[threading.Thread] = field(default_factory=list)
    stopped: bool = False


_state = _GuardState()
_lock = threading.Lock()


# ------------------------------------------------------------------ 心跳


def _beat() -> None:
    """心跳回调——**在事件循环里执行**，只更新一个时间戳（不做任何 IO）。"""
    _state.last_beat = time.monotonic()


def _loop_stalled_ms() -> int:
    """loop 滞后毫秒数（0 = 正常）。"""
    return int((time.monotonic() - _state.last_beat) * 1000)


# ------------------------------------------------------------------ 取证


def _find_pyspy() -> str | None:
    for candidate in PYSPY_CANDIDATES:
        if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _dump_stack(reason: str) -> None:
    """抓一次进程栈（py-spy dump）——把「卡在哪一行」落进日志。

    单独线程执行：dump 本身要几秒，不能挡住看门狗。
    抓不到不算错误（py-spy 可能没装 / ptrace 被禁）——只记日志。
    """
    pyspy = _find_pyspy()
    if pyspy is None:
        logger.warning(
            "事件循环疑似卡住（%s），但容器内没有 py-spy——无法自动取证。"
            "安装：pip3 install py-spy",
            reason,
        )
        return
    try:
        proc = subprocess.run(
            [pyspy, "dump", "--pid", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.error(
            "事件循环疑似卡住（%s）——py-spy dump：\n%s",
            reason,
            proc.stdout or proc.stderr or "(空)",
        )
    except Exception as exc:
        logger.warning("py-spy dump 失败：%s", exc)


# ------------------------------------------------------------------ 挂载探活


def _probe_one(probe: MountProbe) -> None:
    """在**自己的线程**里列一次挂载根——卡死就卡在这里（无害）。"""
    try:
        with os.scandir(probe.path) as it:
            next(iter(it), None)
        probe.status = "ok"
        probe.error = None
    except OSError as exc:
        probe.status = "error"
        probe.error = f"{type(exc).__name__}: {exc}"
    finally:
        probe.checked_at = bj_now()
        probe._inflight = False


def _probe_mounts(mounts: list[str]) -> None:
    """挂载探活线程主体：每 PROBE_INTERVAL_SEC 探一轮。"""
    while not _state.stopped:
        for path in mounts:
            probe = _state.mounts.get(path)
            if probe is None:
                probe = MountProbe(path=path)
                _state.mounts[path] = probe
            if probe._inflight:
                continue  # 上一次还卡着（hard 挂载可能永不返回）——不重复起线程
            probe._inflight = True
            threading.Thread(
                target=_probe_one,
                args=(probe,),
                daemon=True,
                name=f"mount-probe-{path}",
            ).start()
        # 等一轮间隔（挂起时立即可被 stop 打断）
        time.sleep(PROBE_INTERVAL_SEC)


# ------------------------------------------------------------------ 看门狗主体


def _watchdog() -> None:
    """看门狗线程：投递心跳 + 检测 loop 卡顿并自动取证。"""
    while not _state.stopped:
        loop = _state.loop
        if loop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_beat)  # loop 已关闭时忽略
        time.sleep(HEARTBEAT_INTERVAL_SEC)

        stalled_ms = _loop_stalled_ms()
        if stalled_ms >= STALL_THRESHOLD_SEC * 1000:
            if not _state.dumped_for_stall:
                _state.dumped_for_stall = True
                threading.Thread(
                    target=_dump_stack,
                    args=(f"心跳滞后 {stalled_ms}ms",),
                    daemon=True,
                    name="loop-stack-dump",
                ).start()
                logger.error(
                    "事件循环疑似卡住：心跳滞后 %dms——已自动抓栈（py-spy）", stalled_ms
                )
        else:
            _state.dumped_for_stall = False

# ------------------------------------------------------------------ 对外


def start(
    loop: asyncio.AbstractEventLoop | None = None,
    mounts: list[str] | None = None,
) -> None:
    """启动守卫（lifespan 里调用一次）。

    Args:
        loop: 事件循环（默认取当前正在跑的那个）。
        mounts: 要探活的路径列表（如 CIFS 挂载点 / 监听目录）。
    """
    with _lock:
        if _state.threads:
            return  # 已在跑
        _state.stopped = False
        _state.loop = loop or asyncio.get_event_loop()
        for path in mounts or []:
            if path:
                _state.mounts.setdefault(path, MountProbe(path=path))
        _state.last_beat = time.monotonic()

        threads = [
            threading.Thread(target=_watchdog, daemon=True, name="loop-watchdog"),
        ]
        if mounts:
            threads.append(
                threading.Thread(
                    target=_probe_mounts, args=(list(mounts),), daemon=True, name="mount-probe"
                )
            )
        for t in threads:
            t.start()
        _state.threads = threads
    logger.info("运行时守卫已启动：心跳 %ds，探活路径 %s", HEARTBEAT_INTERVAL_SEC, mounts)


def stop() -> None:
    """停止守卫（lifespan 收尾）。"""
    with _lock:
        _state.stopped = True
        _state.threads = []
        _state.loop = None
    logger.info("运行时守卫已停止")


def snapshot() -> dict[str, Any]:
    """当前状态快照——**只读缓存，不做 IO**（供 ``/healthz`` 同步调用）。"""
    stalled_ms = _loop_stalled_ms()
    return {
        "loop_stalled_ms": stalled_ms,
        "loop_ok": stalled_ms < STALL_THRESHOLD_SEC * 1000,
        "mounts": [
            {
                "path": p.path,
                "status": p.status,
                "checked_at": p.checked_at,
                "error": p.error,
            }
            for p in _state.mounts.values()
        ],
    }


__all__ = [
    "HEARTBEAT_INTERVAL_SEC",
    "PROBE_INTERVAL_SEC",
    "STALL_THRESHOLD_SEC",
    "MountProbe",
    "snapshot",
    "start",
    "stop",
]
