"""上传引擎：把本地文件同步到光鸭盘。

**决策表**（与用户确认的口径一致）：

=========================  ==========================================================
云端目标目录的状态           动作
=========================  ==========================================================
无同名文件                 直接上传
同名 + 同大小               **跳过**（幂等——这就是「已同步」的判据）
同名 + 不同大小             **先删旧（进回收站）→ 确认消失 → 传新**
同名，但是个目录            记冲突，**不自动处理**（目录不能被文件覆盖）
本地文件被删除              **不动云端**（设计取舍：不做删除同步）
=========================  ==========================================================

**为什么同名不同大小要「先删再传」**：实测光鸭**同名上传不会覆盖**，
而是自动改名成 ``xxx(1).bin``。要真正覆盖只能先删掉旧的。

**为什么删完要「重列确认」**：``fs_delete`` 是异步任务（返回 taskId），
虽然实测 < 0.5 秒就生效，但不确认就上传可能撞名，产出 ``xxx(1).bin``。
故删后重列目标目录，确认同名已消失再传。

**目录结构**：本地相对路径原样映射到云端（``a/b/c.mkv`` → 目标下
``a/b/c.mkv``）。中间目录用
:class:`~app.services.cloud_index.CloudDirIndex` 逐层创建。

**本地删除是异步的**（2026-10-31）：上传成功后把「删本地」丢后台任务，
worker 立刻处理下一个文件。CIFS 上删一个 6.9 GB 文件要几十秒到几分钟
（等 SMB 服务器释放句柄），同步做会让 worker 长时间空转。
安全校验（fileId / 大小一致 / 路径合法）仍然**同步做**——那是不可绕过的
闸门，不能把「不该删的文件」丢进后台队列。

**删完文件顺手清空目录**（2026-11-03）：用户勾了「同步成功后删除本地
文件」就意味着「传走的东西本地不留痕」→ 空壳目录一并删掉，向上递归到
监听根（不含根）。只用 ``rmdir``（内核保证非空必失败），**绝不**
``rmtree``。详见 :func:`app.services.upload_cleanup.prune_empty_parents`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.drivers.base import DriverError
from app.core.logging import get_logger
from app.services.cloud_index import CloudDirIndex

logger = get_logger(__name__)

#: 后台删除任务的集合（持有引用防 GC）。
#:
#: **必须持有**——``asyncio.create_task`` 的任务没人引用会被 GC 掉，
#: 删除就永远不会执行。
_CLEANUP_TASKS: set[asyncio.Task[None]] = set()

#: 后台删除的并发上限。
#:
#: CIFS 上同时删太多文件会拖垮 NAS（每个删除都是一次网络往返 + 服务器端
#: 句柄释放），故限并发。
_CLEANUP_CONCURRENCY = 3

#: 后台删除的信号量（首次使用时创建——模块级不能建 asyncio 原语）。
_CLEANUP_SEM: asyncio.Semaphore | None = None


def _cleanup_sem() -> asyncio.Semaphore:
    """取后台删除的信号量（懒创建：asyncio 原语要绑事件循环）。"""
    global _CLEANUP_SEM
    if _CLEANUP_SEM is None:
        _CLEANUP_SEM = asyncio.Semaphore(_CLEANUP_CONCURRENCY)
    return _CLEANUP_SEM

#: 单个文件的同步结果
RESULT_UPLOADED = "uploaded"
#: 已存在且大小一致 —— 跳过
RESULT_UNCHANGED = "unchanged"
#: 同名但大小不同 —— 覆盖（删旧传新）
RESULT_OVERWRITTEN = "overwritten"
#: 同名但云端是目录 —— 冲突，不处理
RESULT_CONFLICT = "conflict"
#: 失败
RESULT_FAILED = "failed"
#: 被过滤（临时文件等）
RESULT_SKIPPED = "skipped"


@dataclass
class UploadStats:
    """一次同步（上传）的统计。"""

    uploaded: int = 0
    overwritten: int = 0
    unchanged: int = 0
    conflicts: int = 0
    failed: int = 0
    skipped: int = 0
    #: 实际传输的字节数
    uploaded_bytes: int = 0

    @property
    def total(self) -> int:
        return (
            self.uploaded
            + self.overwritten
            + self.unchanged
            + self.conflicts
            + self.failed
            + self.skipped
        )

    def to_dict(self, **extra: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "uploaded": self.uploaded,
            "overwritten": self.overwritten,
            "unchanged": self.unchanged,
            "conflicts": self.conflicts,
            "failed": self.failed,
            "skipped": self.skipped,
            "uploaded_bytes": self.uploaded_bytes,
            "total": self.total,
        }
        data.update(extra)
        return data


@dataclass
class SyncOutcome:
    """单个文件的同步结果。"""

    status: str
    #: 相对路径（云端视角）
    rel_path: str = ""
    #: 失败原因 / 冲突说明
    reason: str = ""
    #: 本地字节数
    size: int = 0
    #: 云端真实落地的文件名（光鸭可能自动改名）
    cloud_name: str = ""
    #: 云端 fileId（清理本地文件前用它 + 大小做二次校验）
    cloud_file_id: str = ""
    #: 云端字节数（**清理本地前的校验依据**——必须等于 ``size``）
    cloud_size: int = 0
    #: 本地文件是否已被删除（``delete_local`` 开启且成功时）
    local_deleted: bool = False


@dataclass
class SyncContext:
    """一次同步运行的上下文（跨文件共享）。

    Attributes:
        driver: 目标网盘的**驱动**（需 ``list_subdirs``——同名冲突检测用）。
        uploader: 上传器（传字节 / 删文件 / 建目录 / 列目录）。
        index: 目标目录索引。
        root: 本地监听根（绝对路径）。
        delete_retries: 删后重列确认的重试次数。
        delete_delay: 删后重列确认的间隔（秒）。
        delete_local: 上传成功后**清理本地文件**（移进回收目录）。
            **默认关**——破坏性操作，需任务级显式开启。
    """

    driver: Any
    uploader: Any
    index: CloudDirIndex
    root: Path
    delete_retries: int = 10
    delete_delay: float = 0.5
    delete_local: bool = False


async def sync_file(
    ctx: SyncContext,
    local_path: Path,
    rel_path: str,
    on_progress: Any = None,
) -> SyncOutcome:
    """同步单个文件。

    Args:
        ctx: 同步上下文。
        local_path: 本地文件绝对路径。
        rel_path: 相对 ``ctx.root`` 的路径（云端目录结构的依据）。
        on_progress: 上传进度回调 ``(已传字节) -> None``——大文件上传要
            几分钟，没有进度用户会以为卡死了。

    Returns:
        同步结果。**不抛异常**——失败折叠成 ``status=failed``。
    """
    try:
        size = local_path.stat().st_size
    except OSError as exc:
        return SyncOutcome(RESULT_FAILED, rel_path, f"读取本地文件失败：{exc}")

    if size == 0:
        # 0 字节文件不传——多半是空壳。传上去还会在云端留个同名 0 字节
        # 文件，挡住后续的真文件（同名不同大小 → 触发覆盖流程）。
        return SyncOutcome(RESULT_SKIPPED, rel_path, reason="空文件", size=0)

    rel_dir = parent_dir(rel_path)
    file_name = base_name(rel_path)

    try:
        dir_id, existing = await ctx.index.resolve(rel_dir)
    except DriverError as exc:
        return SyncOutcome(RESULT_FAILED, rel_path, f"解析云端目录失败：{exc}")

    remote = existing.get(file_name)

    # ① 同名 + 同大小 → 已同步。
    #
    # **开 ``delete_local`` 时也要删本地**（2026-10-31 修正）：
    # 云端已有同名同大小文件 = 之前传过 = 本地是冗余。开关开着代表用户
    # 已确认「云端是完整副本，本地可删」，故 UNCHANGED 也走清理。
    # 校验同步做、删除异步（见下方第 ⑤ 步的说明）——不绕过校验。
    if remote is not None and remote.size == size:
        outcome = SyncOutcome(
            RESULT_UNCHANGED,
            rel_path,
            size=size,
            cloud_file_id=remote.file_id,
            cloud_size=remote.size,
        )
        if ctx.delete_local:
            note = _check_cleanup_allowed(outcome)
            if note:
                outcome.reason = note
            else:
                _spawn_cleanup(local_path, ctx.root, outcome)
                outcome.local_deleted = True
        return outcome

    # ② 同名但云端是目录 → 冲突，不自动处理
    if remote is None and await _cloud_has_dir(ctx, dir_id, file_name):
        return SyncOutcome(
            RESULT_CONFLICT,
            rel_path,
            reason="云端同名项是目录，无法覆盖",
            size=size,
        )

    # ③ 同名但大小不同 → 覆盖：先删旧，确认消失，再传新
    if remote is not None:
        if not remote.file_id:
            return SyncOutcome(
                RESULT_FAILED,
                rel_path,
                reason="云端旧文件缺少 fileId，无法安全删除（宁可不覆盖）",
                size=size,
            )
        try:
            await ctx.uploader.delete_file(remote.file_id)
        except DriverError as exc:
            return SyncOutcome(RESULT_FAILED, rel_path, f"删除云端旧文件失败：{exc}")

        gone = await _wait_absent(ctx, rel_dir, file_name)
        if not gone:
            return SyncOutcome(
                RESULT_FAILED,
                rel_path,
                reason="删除云端旧文件后它仍然存在（已放弃，避免产出重名副本）",
                size=size,
            )
        await ctx.index.note_removed(rel_dir, file_name)

    # ④ 传
    try:
        result = await ctx.uploader.upload_file(
            local_path, dir_id, name=file_name, progress=on_progress
        )
    except DriverError as exc:
        return SyncOutcome(RESULT_FAILED, rel_path, f"上传失败：{exc}", size=size)

    await ctx.index.note_uploaded(rel_dir, result.name, result.size, result.file_id)

    status = RESULT_OVERWRITTEN if remote is not None else RESULT_UPLOADED
    outcome = SyncOutcome(
        status,
        rel_path,
        size=size,
        cloud_name=result.name,
        cloud_file_id=result.file_id,
        cloud_size=result.size,
    )

    # ⑤ 清理本地文件（**可选的破坏性操作**）
    #
    # 只有 ``ctx.delete_local`` 开启时才做，且必须过三道校验。
    # **失败只记提示，不翻转结果**——上传已经成功了。
    #
    # **删除是异步的**（2026-10-31）：校验同步做完（安全闸门），
    # 真正的 unlink 丢后台任务——CIFS 上删大文件要几十秒到几分钟，
    # 同步做会让 worker 空转。
    if ctx.delete_local:
        note = _check_cleanup_allowed(outcome)
        if note:
            outcome.reason = note
        else:
            _spawn_cleanup(local_path, ctx.root, outcome)
            # 语义：已**安排**删除（不是「已删除」）——实际删除在后台完成。
            outcome.local_deleted = True

    return outcome


def _check_cleanup_allowed(outcome: SyncOutcome) -> str:
    """删本地前的**云端侧**校验。返回原因（空串 = 可以删）。

    1. 云端 fileId 非空
    2. 云端大小 == 本地大小（不只是「API 返回成功」）

    第 3 道（路径合法 / 普通文件 / 非隐藏）在
    :func:`app.services.upload_cleanup.is_safe_to_delete` 里——它要读文件
    系统属性，与实际删除放在一起做（后台任务里做）。
    """
    if not outcome.cloud_file_id:
        return "本地未删除：云端未返回 fileId"
    if outcome.cloud_size != outcome.size:
        return (
            f"本地未删除：云端 {outcome.cloud_size} 字节 / 本地 {outcome.size} 字节不一致"
        )
    return ""


def _spawn_cleanup(local_path: Path, root: Path, outcome: SyncOutcome) -> None:
    """把「删本地」丢后台任务（fire-and-forget）。

    worker 立刻返回处理下一个文件——不等删除完成。
    """
    task = asyncio.create_task(_cleanup_worker(local_path, root, outcome))
    # 持有引用：没人引用的 task 会被 GC 掉，删除就永远不执行。
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)


async def _cleanup_worker(local_path: Path, root: Path, outcome: SyncOutcome) -> None:
    """后台删本地文件。

    **不抛异常**——上传已经成功了，删除失败不该翻转结果。
    失败时本地文件残留 → **下次全量同步会自动重试**（云端已有 →
    UNCHANGED → 再删）。
    """
    from app.services import upload_cleanup

    try:
        async with _cleanup_sem():
            result = await asyncio.to_thread(
                upload_cleanup.delete_local_file, local_path, root
            )
        if result.status in (upload_cleanup.DELETE_OK, upload_cleanup.DELETE_MISSING):
            # MISSING = 文件已被别处删了，不算错
            logger.info("已删除本地文件 %s", local_path)
            # 空目录清理的审计（删磁盘数据不可逆 → 必须能回答「删了什么」）
            for directory in result.removed_dirs:
                logger.info("已删除空目录 %s", directory)
            return
        logger.warning(
            "后台删除本地文件未成功 %s：%s", local_path, result.reason or result.status
        )
    except Exception:
        logger.exception("后台删除本地文件失败：%s", local_path)


async def drain_cleanup_tasks(wait_sec: float = 30.0) -> int:
    """等待后台删除任务完成（停机时调用）。

    停机时等一下，保证「停机后本地文件状态确定」——不留「以为删了其实
    还在」的中间态。

    Args:
        wait_sec: 最长等待秒数。

    Returns:
        实际完成的任务数。
    """
    pending = [t for t in _CLEANUP_TASKS if not t.done()]
    if not pending:
        return 0
    done, still_pending = await asyncio.wait(pending, timeout=wait_sec)
    for t in still_pending:
        t.cancel()
    if still_pending:
        logger.warning(
            "后台删除任务未在 %.0fs 内完成，已取消 %d 个（本地文件将残留）",
            wait_sec,
            len(still_pending),
        )
    return len(done)


async def _wait_absent(ctx: SyncContext, rel_dir: str, name: str) -> bool:
    """等云端目录里的同名文件消失。

    失败即放弃覆盖——**宁可不覆盖也不产出 ``xxx(1).bin`` 重名副本**
    （那会让云端目录逐渐被垃圾副本撑爆）。
    """
    for _ in range(ctx.delete_retries):
        files = await ctx.index.refresh(rel_dir)
        if name not in files:
            return True
        await asyncio.sleep(ctx.delete_delay)
    files = await ctx.index.refresh(rel_dir)
    return name not in files


async def _cloud_has_dir(ctx: SyncContext, dir_id: str, name: str) -> bool:
    """云端目标目录下是否有同名**目录**。"""
    lister = getattr(ctx.driver, "list_subdirs", None)
    if lister is None:
        return False
    try:
        subdirs = await lister(dir_id)
    except DriverError:
        return False
    return any(d.name == name for d in subdirs)


def parent_dir(rel_path: str) -> str:
    """相对路径的父目录。"""
    head, _, _ = rel_path.rpartition("/")
    return head


def base_name(rel_path: str) -> str:
    """相对路径的文件名。"""
    _, _, tail = rel_path.rpartition("/")
    return tail


__all__ = [
    "RESULT_CONFLICT",
    "RESULT_FAILED",
    "RESULT_OVERWRITTEN",
    "RESULT_SKIPPED",
    "RESULT_UNCHANGED",
    "RESULT_UPLOADED",
    "SyncContext",
    "SyncOutcome",
    "UploadStats",
    "base_name",
    "drain_cleanup_tasks",
    "parent_dir",
    "sync_file",
]
