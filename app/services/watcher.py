"""本地目录监听（watchfiles）。

**为什么默认轮询**：生产路径常常是 **CIFS / FUSE** 挂载（Unraid 的
``/mnt/user`` 是 shfs，媒体库走的是 172 的 CIFS 共享）。这些文件系统
**不支持 inotify**——用 inotify 模式会「一个事件都收不到」，看起来像
任务坏了。故默认 ``force_polling=True``，由配置
``UPLOAD_WATCH_POLLING`` 切换。

**稳态判定**：事件触发时文件可能只写了一半。故收到事件后**不立即传**，
而是把文件放进「待确认」表，隔 ``upload_stable_check_sec`` 再采一次样，
size + mtime 都没变才投递给上传队列。

**去重**：同一路径可能触发多次事件（新增 / 修改 / 多次修改）。用
``{path: 上次采样}`` 表天然去重——重复事件只是刷新采样。

**删除事件不处理**：用户明确「本地删了不动云端」。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.services.upload_filter import (
    FileStamp,
    is_empty,
    relative_to_root,
    should_skip,
)

logger = get_logger(__name__)

#: watchfiles 的事件类型（用字符串常量，避免顶层 import 失败时整个模块挂掉）
CHANGE_ADDED = 1
CHANGE_MODIFIED = 2
CHANGE_DELETED = 3


@dataclass
class WatchStats:
    """监听器的统计。"""

    #: 收到的原始事件数
    events: int = 0
    #: 判定为「写完了」并投递的文件数
    dispatched: int = 0
    #: 被过滤掉的文件数（临时文件 / 隐藏文件 / 空文件）
    filtered: int = 0
    #: 软链逃逸等安全问题
    rejected: int = 0
    #: 上次事件时间（unix 秒）
    last_event_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "dispatched": self.dispatched,
            "filtered": self.filtered,
            "rejected": self.rejected,
            "last_event_at": self.last_event_at,
        }


@dataclass
class _Pending:
    """待确认稳态的文件。

    Attributes:
        stamp: 上一次采样（size + mtime）。
        changed_at: **最后一次观察到变化**的时间（unix 秒）。
            稳态判定要求「距最后一次变化至少过了 ``stable_check_sec``」——
            否则写入方还在缓冲区里（size 还没变）时会被误判成稳态，
            传出去一个残缺文件。
    """

    stamp: FileStamp
    changed_at: float


@dataclass
class Watcher:
    """一个监听器（对应一个上传任务）。

    Attributes:
        root: 监听目录（绝对路径）。
        include_subdirs: 是否递归。
        polling: 是否用轮询模式。
        poll_ms: 轮询间隔（毫秒）。
        debounce_ms: 事件防抖窗口（毫秒）。
        stable_check_sec: 稳态判定间隔（秒）。
        dispatch: 投递回调 ``(Path) -> Awaitable[None]``——把稳态的文件
            交给上传队列。
    """

    root: Path
    include_subdirs: bool = True
    polling: bool = True
    poll_ms: int = 1000
    debounce_ms: int = 1500
    stable_check_sec: float = 1.5
    dispatch: Callable[[Path], Any] = field(default=lambda _p: asyncio.sleep(0))
    #: 时钟（测试可注入 —— 稳态判定依赖时间差）
    clock: Callable[[], float] = field(default=time.monotonic)

    stats: WatchStats = field(default_factory=WatchStats)
    #: 待确认稳态的文件
    _pending: dict[Path, _Pending] = field(default_factory=dict)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None

    # -- 生命周期 --

    async def start(self) -> None:
        """启动监听协程。"""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止监听。"""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @property
    def running(self) -> bool:
        """是否在监听。"""
        return self._task is not None and not self._task.done()

    # -- 主循环 --

    async def _loop(self) -> None:
        """监听主循环。

        两个并行协程：

        - ``_watch_events``：收事件 → 进待确认表
        - ``_check_stable``：定时检查待确认表 → 稳态就投递
        """
        try:
            await asyncio.gather(
                self._watch_events(),
                self._check_stable(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("监听协程异常退出：%s", self.root)

    async def _watch_events(self) -> None:
        """收事件。"""
        from watchfiles import awatch

        logger.info("开始监听目录：%s（%s）", self.root, "轮询" if self.polling else "inotify")
        try:
            async for changes in awatch(
                self.root,
                recursive=self.include_subdirs,
                force_polling=self.polling,
                poll_delay_ms=self.poll_ms,
                debounce=self.debounce_ms,
                stop_event=self._stop,
                raise_interrupt=False,
            ):
                for change, raw_path in changes:
                    await self._on_event(change, Path(raw_path))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("监听目录失败：%s", self.root)

    async def _on_event(self, change: int, path: Path) -> None:
        """处理单个事件。"""
        self.stats.events += 1
        self.stats.last_event_at = int(time.time())

        # 删除事件不处理（本地删了不动云端）
        if change == CHANGE_DELETED:
            self._pending.pop(path, None)
            return

        if change not in (CHANGE_ADDED, CHANGE_MODIFIED):
            return

        try:
            reason = should_skip(path, root=self.root)
        except OSError:
            reason = "路径不可读"
        if reason is not None:
            if reason in ("软链逃出监听根",):
                self.stats.rejected += 1
                logger.warning("拒绝监听根外的路径：%s（%s）", path, reason)
            else:
                self.stats.filtered += 1
            self._pending.pop(path, None)
            return

        # 记一次采样，等下一次采样确认稳态
        stamp = FileStamp.of(path)
        if stamp is None:
            self._pending.pop(path, None)
            return
        self._pending[path] = _Pending(stamp=stamp, changed_at=self.clock())

    async def _check_stable(self) -> None:
        """定时检查待确认表，稳态的投递出去。"""
        interval = max(0.2, self.stable_check_sec)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return  # 被要求停止
            except TimeoutError:
                pass

            await self._check_stable_once()

    async def _check_stable_once(self) -> None:
        """检查一轮待确认表（稳态的投递出去）。

        **稳态的三个条件都要满足**：

        1. size + mtime 与上次采样一致（内容没再变）
        2. 距**最后一次变化**至少过了 ``stable_check_sec``
        3. 文件还在

        条件 2 不可省：写入方可能先 ``open`` 再慢慢写，此时 size 还没变，
        条件 1 就会误判成稳态 → 传出残缺文件。故要求「距最后一次观察
        到变化至少一个周期」。

        单独抽出来是为了**可测**——主循环是死循环，测不了。
        """
        now = self.clock()
        min_age = max(0.2, self.stable_check_sec)

        for path in list(self._pending.keys()):
            pending = self._pending.get(path)
            if pending is None:
                continue

            current = FileStamp.of(path)
            if current is None:
                # 文件没了（被删）——移出待确认
                self._pending.pop(path, None)
                continue

            if (
                pending.stamp.size != current.size
                or pending.stamp.mtime != current.mtime
            ):
                # 内容还在变 —— 更新采样，重置计时
                self._pending[path] = _Pending(stamp=current, changed_at=now)
                continue

            if now - pending.changed_at < min_age:
                # 内容没变，但还没「静默」够久 —— 再等一轮
                continue

            # 稳态 —— 投递
            self._pending.pop(path, None)
            if is_empty(path):
                # 稳态了还是 0 字节 → 空壳文件，不值得传
                self.stats.filtered += 1
                continue
            try:
                await self.dispatch(path)
                self.stats.dispatched += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("投递文件失败：%s", path)

    # -- 手动触发 --

    async def dispatch_existing(self) -> int:
        """把目录里**已存在**的文件全部投递（用于首次启动补偿）。

        watchfiles 只报「变化」，**不报存量**。容器重启期间的漏掉的文件
        要靠全量扫描补齐。

        **惰性遍历 + 不阻塞事件循环**：CIFS 上 ``os.walk`` 极慢，物化成
        list 会让启动阻塞几小时。边扫边投——第一个文件扫到就投出去。

        Returns:
            投递的文件数。
        """
        from app.services.upload_filter import aiter_local_files

        count = 0
        async for path in aiter_local_files(
            self.root, include_subdirs=self.include_subdirs
        ):
            try:
                await self.dispatch(path)
                count += 1
                self.stats.dispatched += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("投递文件失败：%s", path)
        return count

    def relative(self, path: Path) -> str:
        """相对路径（云端目录结构的依据）。"""
        return relative_to_root(path, self.root)


__all__ = [
    "CHANGE_ADDED",
    "CHANGE_DELETED",
    "CHANGE_MODIFIED",
    "WatchStats",
    "Watcher",
]
