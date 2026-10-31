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
    # 复用 :func:`_maybe_cleanup_local` 的三重校验（fileId / 大小一致 /
    # 路径合法）——不绕过。
    if remote is not None and remote.size == size:
        outcome = SyncOutcome(
            RESULT_UNCHANGED,
            rel_path,
            size=size,
            cloud_file_id=remote.file_id,
            cloud_size=remote.size,
        )
        if ctx.delete_local:
            note = await _maybe_cleanup_local(ctx, local_path, outcome)
            if note:
                outcome.reason = note
            else:
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
    if ctx.delete_local:
        note = await _maybe_cleanup_local(ctx, local_path, outcome)
        if note:
            outcome.reason = note
        else:
            outcome.local_deleted = True

    return outcome


async def _maybe_cleanup_local(
    ctx: SyncContext,
    local_path: Path,
    outcome: SyncOutcome,
) -> str:
    """上传成功后**彻底删除**本地文件。返回提示信息（空串 = 正常）。

    **三重校验**（任一不过就不删）：

    1. 云端 fileId 非空
    2. 云端大小 == 本地大小（不只是「API 返回成功」）
    3. 路径在监听根内、是普通文件、非隐藏

    **不抛异常**——上传已经成功了，删除失败不该翻转结果。
    """
    from app.services import upload_cleanup

    # ① + ② 云端确认：fileId 有值且大小一致
    if not outcome.cloud_file_id:
        return "本地未删除：云端未返回 fileId"
    if outcome.cloud_size != outcome.size:
        return (
            f"本地未删除：云端 {outcome.cloud_size} 字节 / 本地 {outcome.size} 字节不一致"
        )

    # ③ 彻底删除（unlink —— 不可逆，故前面两道校验很关键）
    #
    # **必须 ``asyncio.to_thread``**：``unlink`` 在 CIFS / NFS 上是同步 IO，
    # 删除大文件会阻塞事件循环（实测：CIFS 删除一个 mkv 卡住整个事件循环，
    # healthcheck 超时、API 不可达）。放线程池里跑不阻塞 loop。
    result = await asyncio.to_thread(
        upload_cleanup.delete_local_file, local_path, ctx.root
    )
    if result.status in (upload_cleanup.DELETE_OK, upload_cleanup.DELETE_MISSING):
        # MISSING = 文件已被别处删了，不算错
        return ""
    if result.status == upload_cleanup.DELETE_REJECTED:
        return f"本地未删除：{result.reason}"
    return f"本地删除失败：{result.reason}"


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
    "parent_dir",
    "sync_file",
]
