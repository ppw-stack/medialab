"""归拢 / 迁移共用的**单文件传输管线**（services 层内部复用）。

## 为什么抽出来

「副盘 → 主盘归拢」（:mod:`app.services.aggregate_sync`）与「手动迁移」
（:mod:`app.services.migration`）走的是**同一条链路**：

```
取直链 → 提交云下载 → 等任务结束 → 改名（光鸭落盘名是 download）→ 校验落盘 → （可选）清理源
```

两份实现必然漂移——而这条链路的每一步都有实测坑（落盘名撞车、索引延迟、
额度硬墙）。故把**与业务无关的管线件**抽到这里，两个业务各自编排。

## 管线件

- :class:`TargetIndex` —— 目标目录索引（目录 id + 文件清单缓存）
- :class:`TaskWatcher` —— 云下载任务的活跃列表轮询（**不读 progress**）
- :func:`norm_dir` —— 相对目录规范化

## 光鸭源的两条铁律（贯穿全管线）

1. **落盘名是 URL 末段**（光鸭 CDN 直链末段 = ``download``）→ 必须改名回原名
2. **恒串行**（并发 1）——同目录并发提交会触发服务端自动改名
   ``download(1)``，改名完成前不能提交下一个
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from collections.abc import Callable
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["SubmitGate", "TargetIndex", "TaskWatcher", "norm_dir"]

#: 轮询离线任务状态的间隔（秒）。
POLL_INTERVAL_SEC = 5.0

#: 「心跳」日志的间隔（秒）——超过这么久没有新日志就记一条当前在途状态。
HEARTBEAT_INTERVAL_SEC = 30.0

#: 任务从活跃列表「消失」几次才认定结束。
#:
#: 刚提交的任务可能还没出现在列表里，一次缺席就判结束会误杀。
ABSENT_CONFIRM = 2

#: 任务结束后校验落盘的重试次数与间隔。
#:
#: 文件索引有延迟：刚完成时目录里可能还查不到，验早了会误报失败。
VERIFY_RETRIES = 3
VERIFY_DELAY_SEC = 5.0

#: 云下载落盘的默认文件名——光鸭 CDN 直链的 URL 末段（实测 2026-11-13：
#: ``https://<pool>-httpdown.guangyacdn.com/download/?fid=...``）。
#:
#: 云下载按 URL 末段落名，故光鸭源的每个文件都先落成这个名字，再改名。
GUANGYA_LANDED_NAME = "download"


def norm_dir(rel: str) -> str:
    """相对目录规范化（去首尾斜杠、合并空段）。"""
    return "/".join(p for p in rel.strip("/").split("/") if p)


class TargetIndex:
    """目标目录的 fileId + 文件清单缓存。

    一个 run 内共享：同一部影片目录会被多个文件命中，每个目录只查一次。
    并发 worker 共用一个锁——目录解析是低频操作（每个目录一次），串行
    反而避免了「两个 worker 同时给同一目录建副本」的竞态。
    """

    def __init__(self, driver: Any, root_id: str) -> None:
        self._driver = driver
        self._root = str(root_id or "")
        self._dir_ids: dict[str, str] = {"": self._root}
        self._files: dict[str, dict[str, int]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, rel_dir: str) -> tuple[str, dict[str, int]]:
        """解析相对目录，返回 ``(fileId, {文件名: 大小})``（不存在就建）。

        返回的是缓存的**副本**——调用方拿到后随便改都不会污染缓存。
        """
        rel = norm_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            files = self._files.get(rel)
            if files is None:
                files = dict(await self._driver.list_files(dir_id))
                self._files[rel] = files
            return dir_id, dict(files)

    async def create_offline_task(self, url: str, dir_id: str) -> str:
        """提交离线下载任务（转发给驱动，便于调用方只依赖 index）。"""
        result: str = await self._driver.create_offline_task(url, dir_id)
        return result

    async def note_submitted(self, rel_dir: str, name: str, size: int) -> None:
        """登记「已提交待下载」的文件。

        **必须更新缓存**——否则后续对同一目录的预检会看不到刚提交的文件，
        缓存与光鸭实际状态不一致（``resolve`` 返回旧数据、``refresh``
        返回新数据，两者矛盾）。将来支持「重试失败项」时还会真的重复提交。
        """
        rel = norm_dir(rel_dir)
        async with self._lock:
            files = self._files.setdefault(rel, {})
            files[name] = size

    async def rename_landed(
        self,
        rel_dir: str,
        landed_names: tuple[str, ...],
        size: int,
        new_name: str,
    ) -> str:
        """把「已落盘但还没改名」的文件改名成 ``new_name``。

        云下载按 **URL 末段**落名——115 直链末段是文件名（落名正确），
        光鸭直链末段是 ``download``（实测），故光鸭源必须改名。

        匹配条件（都要满足，避免误改用户自己的文件）：

        1. 文件名是 ``landed_names`` 里的名字，或它的服务端自动改名形态
           （``download(1)``）
        2. **字节数完全一致**
        3. 目标名与现名不同

        Args:
            rel_dir: 目标下的相对目录。
            landed_names: 可能的落盘名（一般只有 ``download``）。
            size: 期望的字节数。
            new_name: 改成的目标名。

        Returns:
            改成的文件名；没找到可认领的文件则返回空串。
        """
        rel = norm_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            entries: dict[str, tuple[int, str]] = await self._driver.list_entries(
                dir_id
            )
            for name, (entry_size, file_id) in entries.items():
                if entry_size != size:
                    continue
                if name == new_name:
                    continue
                if not any(
                    name == candidate or name.startswith(f"{candidate}(")
                    for candidate in landed_names
                ):
                    continue
                await self._driver.rename_file(file_id, new_name)
                files = self._files.setdefault(rel, {})
                files.pop(name, None)
                files[new_name] = size
                return name
            return ""

    async def delete_replaced(self, rel_dir: str, name: str, size: int) -> bool:
        """删除目标侧「同名但更小」的文件（同名不同大小、保留大的时用）。

        进回收站（可恢复）。找不到同名同大小的文件 → 返回 False（不删）。
        """
        rel = norm_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            entries: dict[str, tuple[int, str]] = await self._driver.list_entries(
                dir_id
            )
            hit = entries.get(name)
            if hit is None or hit[0] != size:
                return False
            await self._driver.delete_file(hit[1])
            files = self._files.setdefault(rel, {})
            files.pop(name, None)
            return True

    async def failure_reason(self, task_id: str) -> str:
        """查光鸭给出的失败原因（查不到就返回空串）。

        只在「任务已结束但目标目录没有文件」时调用——正常路径不额外花请求。
        """
        try:
            items = await self._driver.list_failed_offline_tasks()
        except Exception:
            return ""
        for item in items:
            if str(item.get("taskId")) != task_id:
                continue
            message = str(item.get("errMsg") or "").strip()
            code = item.get("errCode")
            if not message:
                continue
            return f"光鸭离线下载失败：{message}" + (
                f"（code={code}）" if code is not None else ""
            )
        return ""

    async def refresh(self, rel_dir: str) -> dict[str, int]:
        """**重新**列目录（任务结束后校验落盘用，绕过缓存）。"""
        rel = norm_dir(rel_dir)
        async with self._lock:
            dir_id = await self._resolve_dir(rel)
            files: dict[str, int] = dict(await self._driver.list_files(dir_id))
            self._files[rel] = files
            return dict(files)

    async def _resolve_dir(self, rel: str) -> str:
        cached = self._dir_ids.get(rel)
        if cached is not None:
            return cached

        parent_rel = posixpath.dirname(rel)
        name = posixpath.basename(rel)
        parent_id = await self._resolve_dir(parent_rel)
        dir_id, created = await self._driver.create_dir(name, parent_id)
        dir_id = str(dir_id)
        self._dir_ids[rel] = dir_id
        if created:
            # 新建目录必定为空，省掉一次列表请求
            self._files[rel] = {}
        return dir_id


class SubmitGate:
    """全局提交闸门：保证两次提交之间至少间隔 N 秒。

    **为什么要全局**（而不是每个 worker 各自 sleep）：worker 是并发的，
    各自 sleep 仍可能撞在同一时刻。闸门用一把锁串行化「排队 + 计时」，
    保证实际提交时刻彼此至少相隔 ``interval``。

    **先过闸门再换链**：直链是「新鲜的」，光鸭收到就能开始拉；
    若是先换链再等，链接可能在队列里放几十秒才开始下载。
    """

    def __init__(self, interval: float) -> None:
        self._interval = max(0.0, float(interval))
        self._next_at: float = 0.0
        self._lock = asyncio.Lock()
        #: 累计等待秒数（给日志/统计用）
        self.total_waited = 0.0

    async def wait(self) -> float:
        """等到可以提交为止，返回本次实际等待的秒数。"""
        if self._interval <= 0:
            return 0.0
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay > 0:
                await asyncio.sleep(delay)
            # 等待结束后才推进「下次可提交时刻」
            self._next_at = time.monotonic() + self._interval
            self.total_waited += delay
            return delay


class TaskWatcher:
    """共享的离线任务轮询器（一个 run 一个）。

    只判断「任务还在不在活跃列表里」——**不读 progress 判定完成、不设超时**。

    **为什么不在途进度只报文件路径**：光鸭的 ``progress`` 字段语义不可靠
    （实测它不是百分比，任务项初始甚至没有这个字段，只在真实拉取时出现），
    展示它只会误导用户——大文件实际下到 88% 时它可能显示个位数。完成判定
    走「任务从活跃列表消失」+「目标目录出现同名同大小文件」，与该字段无关。

    日志通过注入的 ``log`` 回调输出（不依赖具体作业对象——归拢引擎与手动
    迁移各自的日志去处不同）。
    """

    def __init__(
        self,
        driver: Any,
        *,
        log: Callable[[str], None] | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._driver = driver
        self._log = log or (lambda _msg: None)
        self._pending: dict[str, asyncio.Event] = {}
        self._seen: set[str] = set()
        self._absent: dict[str, int] = {}
        self._in_flight: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopped: bool = False
        self._last_heartbeat = time.monotonic()
        if poll_interval is None:
            from app.core.config import get_settings

            poll_interval = get_settings().migration_poll_interval_sec
        self._poll_interval = max(1.0, float(poll_interval))

    async def wait(
        self,
        task_id: str,
        *,
        path: str = "",
        size: int = 0,
    ) -> None:
        """等一个任务结束（从活跃列表消失）。"""
        event = asyncio.Event()
        self._pending[task_id] = event
        if path:
            self._in_flight[task_id] = {"path": path, "size": size}
        self._ensure_running()
        try:
            await event.wait()
        finally:
            self._pending.pop(task_id, None)
            self._seen.discard(task_id)
            self._absent.pop(task_id, None)
            self._in_flight.pop(task_id, None)

    @property
    def in_flight_items(self) -> dict[str, dict[str, Any]]:
        """当前在途任务（``task_id`` → ``{path, size}``）。"""
        return dict(self._in_flight)

    def _ensure_running(self) -> None:
        if self._stopped:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stopped and self._pending:
            await asyncio.sleep(self._poll_interval)
            try:
                items = await self._driver.list_active_offline_tasks()
            except Exception as exc:  # 轮询失败不致命，下一轮再来
                self._log(f"⚠️ 查询离线任务失败（下轮重试）：{exc}")
                continue

            active: set[str] = set()
            for item in items:
                task_id = str(item.get("taskId") or "")
                if not task_id:
                    continue
                active.add(task_id)

            for task_id, event in list(self._pending.items()):
                if task_id in active:
                    self._seen.add(task_id)
                    self._absent[task_id] = 0
                    continue
                self._absent[task_id] = self._absent.get(task_id, 0) + 1
                if task_id in self._seen or self._absent[task_id] >= ABSENT_CONFIRM:
                    event.set()

            self._maybe_heartbeat()

    async def close(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _maybe_heartbeat(self) -> None:
        """长时间没有新日志时，记一条当前在途状态（证明还活着）。

        **每个任务一行**——早先把多个任务挤在一行，结果日志框里换行错乱、
        完全看不出哪个是哪个（截图里就是这样）。且用**完整路径**，只看文件名
        分不清是哪部剧。
        """
        now = time.monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SEC:
            return
        self._last_heartbeat = now
        items = self.in_flight_items
        if not items:
            return
        self._log(f"⏱️ 仍在下载（{len(items)} 个）：")
        for item in items.values():
            # 用「│」做缩进标记——不能用空格（前端剥时间戳时会把前导空白吃掉）
            self._log(f"│ {item['path']}")
