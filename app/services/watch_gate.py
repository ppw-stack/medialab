"""数据同步小文件通道的动态并发闸与队列条目。

从 :mod:`app.services.watch_manager` 拆出的纯并发原语模块（零 DB / 网络耦合）：

- :class:`WatchItem`：队列里的待上传稳态文件条目（路径 + 入队 size）。
- :class:`SmallGate`：小文件通道动态并发闸（大文件上传让路 + 连续失败熔断
  + 冷却 + 半开串行观察恢复）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from pathlib import Path

#: 小通道熔断：连续失败 N 次 → 降为串行 + 冷却
#:
#: ``Server disconnected`` / 卡死是实测的并发症状——连续失败大概率是
#: 服务端扛不住，立即止损。
BREAKER_FAIL_STREAK = 3

#: 熔断冷却（秒）
BREAKER_COOLDOWN_SEC = 60.0

#: 熔断恢复：降级期间**连续成功** N 次才重新放开并发
#: （防止「刚恢复就再次打挂服务端」的振荡）
BREAKER_RECOVER_SUCCESS = 10


@dataclass(slots=True)
class WatchItem:
    """一条待上传的稳态文件（路径 + 入队时的 size）。

    size 是**分流依据**（稳态采样时已拿到，不二次 stat），也用于进度
    显示；权威大小由 :func:`app.services.uploader.sync_file` 现场比对。
    """

    path: Path
    size: int


class SmallGate:
    """小文件通道的**动态并发闸**。

    K 个 worker 常驻（K = 任务 ``concurrency`` 列），同一时刻最多
    ``effective_limit()`` 个 worker 在传：

    - **大文件上传中 → 1**：小文件的字节占比极小，但并发请求会与
      大文件抢限速配额与带宽——保守起见让路（大文件传完立即恢复 K）。
    - **熔断 → 1 + 冷却**：连续失败 3 次 → 冷却 60s（期间串行）；
      冷却结束后**仍是串行**，连续成功 10 次才重新放开并发——
      避免「刚恢复就再次打挂服务端」的振荡。

    实现用 ``asyncio.Condition`` + 定时重查：冷却到期**没有人 notify**
    （没有定时器），靠 0.5s 的 ``wait_for`` 超时兜底唤醒重查。
    """

    def __init__(self, k: int) -> None:
        self._cond = asyncio.Condition()
        #: 静态并发上限（任务 concurrency 列）
        self.k = max(1, int(k))
        #: 大文件正在上传（大文件 worker 置位 / 复位）
        self.big_active = False
        #: 熔断降级中（连续成功达标后解除）
        self.derated = False
        #: 熔断冷却截止时刻（monotonic 秒）
        self.resume_at = 0.0
        #: 连续失败计数（成功清零）
        self.fail_streak = 0
        #: 降级期间的连续成功计数
        self.success_streak = 0
        #: 当前持闸的 worker 序号
        self._active: set[int] = set()

    def effective_limit(self) -> int:
        """当前允许的并发数。"""
        if time.monotonic() < self.resume_at:
            return 1  # 冷却中
        if self.derated:
            return 1  # 熔断恢复期，先串行观察
        if self.big_active:
            return 1  # 大文件上传中，让路
        return self.k

    async def acquire(self, ordinal: int) -> None:
        """占一个并发名额（满了就等：被 notify 或 0.5s 超时后重查）。"""
        async with self._cond:
            while len(self._active) >= self.effective_limit():
                # 冷却到期 / 限值变化没人 notify——0.5s 超时兜底唤醒重查
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._cond.wait(), timeout=0.5)
            self._active.add(ordinal)

    async def release(self, ordinal: int) -> None:
        """归还并发名额并唤醒等待者。"""
        async with self._cond:
            self._active.discard(ordinal)
            self._cond.notify_all()

    async def set_big_active(self, active: bool) -> None:
        """大文件上传开始 / 结束（大文件 worker 调）。"""
        async with self._cond:
            self.big_active = active
            self._cond.notify_all()

    def record_failure(self) -> bool:
        """记录一次失败。返回是否**触发熔断**。"""
        self.fail_streak += 1
        if self.fail_streak < BREAKER_FAIL_STREAK:
            return False
        self.fail_streak = 0
        self.derated = True
        self.success_streak = 0
        self.resume_at = time.monotonic() + BREAKER_COOLDOWN_SEC
        return True

    def record_success(self) -> None:
        """记录一次成功（失败计数清零；降级期间攒够连续成功就恢复并发）。"""
        self.fail_streak = 0
        if self.derated:
            self.success_streak += 1
            if self.success_streak >= BREAKER_RECOVER_SUCCESS:
                self.derated = False
                self.success_streak = 0
