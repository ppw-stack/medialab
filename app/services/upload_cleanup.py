"""上传成功后**彻底删除**本地文件。

**这是不可逆操作**——所以「删」的前置条件非常严格：**只有「云端确认
落盘正确」才删**。任何一步对不上，本地文件原样保留。

## 三重校验（任一不过就不删）

1. 云端 fileId 非空（API 真的返回了落盘结果）
2. **云端大小 == 本地大小**（这是「字节真的传对了」的唯一硬证据——
   API 返回 200 不等于文件完整）
3. 路径安全（见下）

## 路径校验（AGENTS.md：宁可不删也不误删）

- 必须是**普通文件**（不删目录、不删软链）
- 文件名不以 ``.`` 开头（不碰隐藏文件）
- 必须在**监听根内**（`realpath` 校验，防软链逃逸）

## 空目录清理（删完文件顺手删空壳目录）

文件删完后，它所在的目录可能空了。**空目录一并删掉**，向上递归
直到监听根（**不含**监听根本身）。

**为什么用 ``rmdir`` 而不是 ``rmtree``**（关键决策）：

``os.rmdir`` **只能删空目录**——非空必然报 ``ENOTEMPTY``。这是**内核级
保证**，没有「先 list 再判断空」的 TOCTOU 窗口（检查与删除之间不可能
塞进新文件）。这正是 AGENTS.md「不误删」底线要求的形态。

``rmtree`` **绝不用于此处**——它会递归删非空目录，那是灾难性后果。

**四道安全闸**（每个候选目录逐个校验，见 :func:`is_safe_to_prune_dir`）：

1. 不是监听根本身（监听根是用户指定的骨架，必须保留）
2. ``realpath`` 在监听根内（防软链逃逸）
3. 不是软链（不跟随软链删它指向的真实目录）
4. 相对路径里**任一段**不以 ``.`` 开头（``.strmmanger-trash`` 等隐藏
   目录一律不动）

**errno 分级**（决定是否继续向上，见 :func:`_try_rmdir`）：

| errno | 含义 | 动作 |
|---|---|---|
| ``ENOENT`` | 已被并发任务删了 | **继续向上**（父目录可能也空了） |
| ``ENOTEMPTY`` / ``EEXIST`` | 目录非空 | **停止向上**（父目录必然也非空） |
| 其它（EACCES / CIFS 错误） | 权限 / IO 问题 | 记 warning，**停止向上** |

``ENOTEMPTY`` 会**短间隔重试**：CIFS 上 ``unlink`` 返回后目录项可能
还在服务端缓存，立刻 ``rmdir`` 会误判非空 → 重试 2 次（0.3s）。

## 不删的情形

| 情形 | 原因 |
|---|---|
| ``unchanged``（云端本来就有） | 本地文件是唯一原件，云端有≠可以删 |
| ``overwritten`` 之外的任何失败 | 上传没成功，删了就没了 |
| 云端大小不符 | 可能传了个残缺文件 |
| 云端没返回 fileId | 无法确认落盘 |

## 能不能找回

**不能**——本地文件被 `unlink` 了。但云端有完整副本，需要时可以
从光鸭盘重新下载（面板「媒体库」/「迁移」页都基于云端数据）。
"""

from __future__ import annotations

import errno
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 删除成功的分类
DELETE_OK = "ok"
#: 文件不存在（可能已被别处删了）
DELETE_MISSING = "missing"
#: 路径校验不通过（不在监听根内 / 是目录 / 是软链 / 隐藏文件）
DELETE_REJECTED = "rejected"
#: 删除失败（权限等）
DELETE_FAILED = "failed"


@dataclass(frozen=True)
class DeleteResult:
    """一次本地删除的结果。"""

    status: str
    #: 被删文件的绝对路径（成功时）
    removed_path: str = ""
    #: 失败 / 拒绝的原因
    reason: str = ""
    #: 顺手删掉的**空父目录**（自下而上，已被删的那些）
    removed_dirs: list[str] = field(default_factory=list)


def is_safe_to_delete(path: Path, root: Path) -> str | None:
    """校验「这个路径能不能删」。返回拒绝原因（``None`` = 可以删）。

    **四道校验**（任一不过就拒）：

    1. 是**普通文件**（不删目录、不删软链）——**先查这个**：文件不存在
       时直接返回，不会误报「逃出监听根」
    2. 文件名不以 ``.`` 开头（不碰隐藏文件 / 临时文件）
    3. 在监听根内（软链逃逸）
    4. 不在回收目录内（历史残留的 ``.strmmanger-trash`` 不删）
    """
    # ① 软链**先查**——它可能指向根外，报「是软链」比「逃出根」更准确
    if path.is_symlink():
        return "是软链，不删"

    # ② 文件不存在 / 不是普通文件
    if not path.is_file():
        return "不是普通文件"

    # ③ 隐藏文件
    if path.name.startswith("."):
        return "隐藏文件，不删"

    # ④ 在监听根内（软链逃逸）
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError as exc:
        return f"路径无法解析：{exc}"

    if resolved != base and not resolved.is_relative_to(base):
        return "软链逃出监听根"

    # ⑤ 历史残留的回收目录（旧版本移进去的）一律不动
    from app.services.upload_filter import TRASH_DIR_NAME

    trash = root / TRASH_DIR_NAME
    try:
        trash_resolved = trash.resolve()
    except OSError:
        trash_resolved = None
    if trash_resolved is not None and (
        resolved == trash_resolved or resolved.is_relative_to(trash_resolved)
    ):
        return "回收目录内的文件，不删"

    return None


def delete_local_file(path: Path, root: Path) -> DeleteResult:
    """**彻底删除**本地文件（``unlink``，不可逆）。

    删完后**顺手清空目录**：向上递归删掉连续的空父目录（见
    :func:`prune_empty_parents`）。这是不可逆操作的一部分——用户勾了
    「同步成功后删除本地文件」，就意味着「传走的东西本地不留痕」，
    留着空壳目录只是垃圾。

    Args:
        path: 要删除的本地文件。
        root: 监听根（用于路径安全校验）。

    Returns:
        删除结果。**不抛异常**——失败折叠成 ``status``。
    """
    # 文件已经不在 → ``missing``（不算错，可能被别处删了）。
    # **必须在安全校验之前判**——否则「不存在」会被误报成「不是普通文件」。
    if not path.exists() and not path.is_symlink():
        return DeleteResult(DELETE_MISSING)

    reason = is_safe_to_delete(path, root)
    if reason is not None:
        return DeleteResult(DELETE_REJECTED, reason=reason)

    try:
        path.unlink()
    except FileNotFoundError:
        return DeleteResult(DELETE_MISSING)
    except OSError as exc:
        logger.warning("删除本地文件失败 %s：%s", path, exc)
        return DeleteResult(DELETE_FAILED, reason=str(exc))

    logger.info("已删除本地文件 %s", path)
    # **同一线程内接着清空目录**——避免「文件删了目录还没删」的中间态
    # 暴露给监听器（它会看到目录变空又变没）。
    removed_dirs = prune_empty_parents(path.parent, root)
    return DeleteResult(DELETE_OK, removed_path=str(path), removed_dirs=removed_dirs)


def is_safe_to_prune_dir(path: Path, root: Path) -> str | None:
    """校验「这个目录能不能被清空」。返回拒绝原因（``None`` = 可以删）。

    **四道闸**（任一不过就拒）：

    1. **不是监听根本身**——监听根是用户指定的骨架，必须保留
    2. 不是软链（不跟随软链删它指向的真实目录）
    3. 在监听根内（realpath 校验，防软链逃逸）
    4. 相对路径里**任一段**不以 ``.`` 开头
       （``.strmmanger-trash`` 等隐藏目录一律不动）

    与 :func:`is_safe_to_delete` 的区别：那个管**文件**（必须是普通
    文件），这个管**目录**（必须是目录、且不能是根）。
    """
    # ① 软链先查——它可能指向根外
    if path.is_symlink():
        return "是软链，不删"

    # ② 必须是目录
    if not path.is_dir():
        return "不是目录"

    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError as exc:
        return f"路径无法解析：{exc}"

    # ③ 监听根本身不删
    if resolved == base:
        return "是监听根，不删"

    # ④ 在监听根内（软链逃逸）
    if not resolved.is_relative_to(base):
        return "软链逃出监听根"

    # ⑤ 相对路径里任一段是隐藏目录 → 不删
    try:
        rel = resolved.relative_to(base)
    except ValueError:
        return "不在监听根内"
    if any(part.startswith(".") for part in rel.parts):
        return "隐藏目录内，不删"

    return None


def prune_empty_parents(
    start_dir: Path,
    root: Path,
    *,
    retries: int = 2,
    delay: float = 0.3,
) -> list[str]:
    """向上递归删掉**空**目录，直到监听根（**不含**监听根本身）。

    **只删空目录**——用 ``os.rmdir``（内核保证非空必失败），**绝不**
    用 ``rmtree``（那会递归删非空目录，是灾难性后果）。

    Args:
        start_dir: 起点目录（通常是刚删掉的那个文件的父目录）。
        root: 监听根。**它本身永不删除**，且是向上递归的终点。
        retries: 目录非空时的重试次数（CIFS 上 ``unlink`` 返回后目录项
            可能还在服务端缓存，立刻 ``rmdir`` 会误判非空）。
        delay: 重试间隔（秒）。

    Returns:
        被删掉的目录绝对路径列表（自下而上）。
    """
    removed: list[str] = []
    current = start_dir
    # ``resolve()`` 默认非严格——路径不存在也不抛（已被删的目录照样能算）
    base = root.resolve()

    while True:
        # ① 到达监听根 → 停（根永不删除）
        try:
            if current.resolve() == base:
                break
        except OSError:
            break

        # ② 当前目录已不存在 → 被并发任务删了 → **继续向上**
        #    （父目录可能也空了）
        if not current.exists():
            parent = current.parent
            if parent == current:
                break
            current = parent
            continue

        # ③ 安全闸
        reason = is_safe_to_prune_dir(current, root)
        if reason is not None:
            logger.debug("跳过清理目录 %s：%s", current, reason)
            break

        # ④ 删（空目录才删得掉）
        ok, stop = _try_rmdir(current, retries=retries, delay=delay)
        if ok:
            removed.append(str(current))
            logger.info("已删除空目录 %s", current)
        elif stop:
            # 非空 / 权限错误 → **停止向上**（父目录必然也非空）
            logger.debug("目录 %s 未清理，停止向上清理", current)
            break
        # 否则是 ENOENT（已被并发任务删了）→ 幂等，**继续向上**
        # （父目录可能也空了）

        # ⑤ 向上继续
        parent = current.parent
        if parent == current:
            # 到达文件系统根 → 停
            break
        current = parent

    return removed


def _try_rmdir(path: Path, *, retries: int, delay: float) -> tuple[bool, bool]:
    """删一个空目录。

    Returns:
        ``(是否删成功, 是否应停止向上)``。
        - 删成功 → ``(True, False)``（继续向上）
        - 目录非空 → ``(False, True)``（**停**——父目录必然也非空）
        - 已不存在 → ``(False, False)``（继续向上——父目录可能也空了）
        - 其它错误 → ``(False, True)``（停，记日志）
    """
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            path.rmdir()
            return True, False
        except FileNotFoundError:
            # 已被并发任务删了 → 幂等，继续向上
            return False, False
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                # 非空——重试（可能是 CIFS 的目录项缓存）
                if attempt < attempts - 1:
                    time.sleep(delay)
                    continue
                return False, True
            # 权限 / IO 错误 → 停（重试也没用）
            logger.warning("删除空目录失败 %s：%s", path, exc)
            return False, True
    return False, True


__all__ = [
    "DELETE_FAILED",
    "DELETE_MISSING",
    "DELETE_OK",
    "DELETE_REJECTED",
    "DeleteResult",
    "delete_local_file",
    "is_safe_to_delete",
    "is_safe_to_prune_dir",
    "prune_empty_parents",
]
