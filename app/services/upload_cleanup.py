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

from dataclasses import dataclass
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
    return DeleteResult(DELETE_OK, removed_path=str(path))


__all__ = [
    "DELETE_FAILED",
    "DELETE_MISSING",
    "DELETE_OK",
    "DELETE_REJECTED",
    "DeleteResult",
    "delete_local_file",
    "is_safe_to_delete",
]
