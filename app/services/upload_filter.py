"""上传前的文件筛选与「写完」判定。

**两个问题，一个模块解决**：

1. **该不该传这个文件？** —— 临时文件（``.part`` / ``~$`` / ``.DS_Store``）
   不该传；目录不传；超小文件（0 字节）一般也不值得传。

2. **文件写完了吗？** —— 监听器看到「文件出现」时，它可能只写了一半。
   直接传会传出**残缺文件**，而且云端会留下一个大小错误的副本
   （下次比对会误判成「同名不同大小」→ 触发一次无意义的覆盖）。

   判定法：**两次采样之间 size + mtime 都不变**才算稳态。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: 回收目录名（同步成功后清理掉的本地文件的暂存处）。
#:
#: **以 ``.`` 开头**是有意的——:func:`should_skip` 会把隐藏目录整个跳过，
#: 否则回收的文件会被重新监听到、重新上传，形成
#: 「移进回收站 → 又传一遍 → 再移进回收站」的死循环。
#:
#: 定义在**本模块**（更底层）：:mod:`app.services.upload_cleanup` 反过来
#: 从这里导入，避免两个模块循环依赖。
TRASH_DIR_NAME = ".strmmanger-trash"

#: 临时文件的后缀 / 前缀特征
#:
#: - ``.part`` —— 本项目附件下载用的临时后缀（:mod:`app.services.attachments`）
#: - ``.crdownload`` / ``.tmp`` —— 浏览器 / 工具的下载临时文件
#: - ``~$`` 前缀 —— Office / 临时编辑文件
#: - ``.DS_Store`` / ``Thumbs.db`` / ``.gitkeep`` —— 系统的杂项文件
TEMP_SUFFIXES: frozenset[str] = frozenset(
    {
        ".part",
        ".crdownload",
        ".tmp",
        ".temp",
        ".swp",
        ".swx",
        ".ds_store",
        ".thumb.db",
    }
)

#: 临时文件的**前缀**特征（小写比较）
TEMP_PREFIXES: tuple[str, ...] = ("~$", ".~", ".goutputstream-")

#: 隐藏文件（以 ``.`` 开头）是否跳过
SKIP_HIDDEN = True

#: 0 字节文件是否跳过
SKIP_EMPTY = True


@dataclass(frozen=True)
class FileStamp:
    """文件的状态采样（size + mtime）。"""

    size: int
    mtime: float

    @classmethod
    def of(cls, path: Path) -> FileStamp | None:
        """采一次样。文件不存在/取不到 → ``None``。"""
        try:
            stat = path.stat()
        except OSError:
            return None
        return cls(size=stat.st_size, mtime=stat.st_mtime)


def is_temporary(name: str) -> bool:
    """文件名是否是临时文件。"""
    lowered = name.lower()
    if any(lowered.startswith(prefix) for prefix in TEMP_PREFIXES):
        return True
    if lowered.endswith(".gitkeep"):
        return False
    return any(lowered.endswith(suffix) for suffix in TEMP_SUFFIXES)


def should_skip(path: Path, *, root: Path | None = None) -> str | None:
    """判断该路径该不该跳过，返回跳过原因（``None`` = 不跳过）。

    **空文件不在这里判**——真实写入流程是「先建空文件，再往里写」，
    在事件阶段就按空文件跳过会把正在写的文件踢掉。故空文件规则只在
    **稳态判定之后**生效（见 :func:`is_empty`）。

    **隐藏目录要查整条路径**：回收目录 ``.strmmanger-trash`` 里的文件，
    文件名是正常的（``a.mkv``），只看 ``path.name`` 会把它当正常文件
    —— 结果是「移进回收站 → 又被上传一遍 → 再移进回收站」的死循环。
    故检查路径里**任一组件**以 ``.`` 开头。

    Args:
        path: 待判断的路径。
        root: 监听根。给了就检查路径是否**仍在根内**（软链逃逸）。
    """
    name = path.name

    if is_temporary(name):
        return "临时文件"

    # ① 回收目录**优先**判——它是本模块自己创建的，必须绝对排除
    if TRASH_DIR_NAME in path.parts:
        return "回收目录内的文件"

    # ② 隐藏文件 / 隐藏目录（查整条路径，不只文件名）
    if SKIP_HIDDEN:
        if name.endswith(".gitkeep"):
            pass  # 占位文件放行（但通常 0 字节，仍会被空文件规则挡下）
        elif name.startswith("."):
            return "隐藏文件"
        elif any(
            part.startswith(".") for part in path.parts[:-1]
        ):
            # 路径里有隐藏目录（如 ``.strmmanger-trash/xxx/a.mkv``）
            return "隐藏目录内"

    if root is not None:
        try:
            resolved = path.resolve()
            base = root.resolve()
        except OSError:
            return "路径无法解析"
        if resolved != base and not resolved.is_relative_to(base):
            return "软链逃出监听根"

    try:
        path.stat()
    except OSError:
        return "文件不可读"

    if not path.is_file():
        return "不是普通文件"

    return None


def is_empty(path: Path) -> bool:
    """文件是否 0 字节。

    **只在稳态判定之后用**——0 字节文件多半是「写了一半」或「空壳」，
    传上去没意义，还会在云端留个同名 0 字节文件挡住后续的真文件。
    """
    try:
        return path.stat().st_size == 0
    except OSError:
        return True


def is_stable(path: Path, previous: FileStamp | None) -> bool:
    """文件是否「写完了」。

    Args:
        path: 文件路径。
        previous: 上一次采样。``None`` = 第一次看这个文件。

    Returns:
        True = 稳态（可以传）；False = 可能还在写，等下次再看。
    """
    current = FileStamp.of(path)
    if current is None:
        return False
    if previous is None:
        return False
    return previous.size == current.size and previous.mtime == current.mtime


def relative_to_root(path: Path, root: Path) -> str:
    """算相对路径（云端目录结构的依据）。

    用 **posix 风格**分隔符——云端路径一律用 ``/``。
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    return rel.as_posix()


def iter_local_files(
    root: Path,
    *,
    include_subdirs: bool = True,
) -> list[Path]:
    """遍历监听目录下的所有文件（已过滤掉临时文件与目录）。

    用于「立即全量同步」——事件流会漏（容器重启期间），全量比对能补齐。
    """
    results: list[Path] = []
    if not root.is_dir():
        return results

    if include_subdirs:
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过隐藏目录
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            base = Path(dirpath)
            for filename in filenames:
                candidate = base / filename
                if should_skip(candidate, root=root) is None:
                    results.append(candidate)
    else:
        for candidate in root.iterdir():
            if candidate.is_file() and should_skip(candidate, root=root) is None:
                results.append(candidate)
    return results


__all__ = [
    "SKIP_EMPTY",
    "SKIP_HIDDEN",
    "TEMP_PREFIXES",
    "TEMP_SUFFIXES",
    "FileStamp",
    "is_stable",
    "is_temporary",
    "iter_local_files",
    "relative_to_root",
    "should_skip",
]
