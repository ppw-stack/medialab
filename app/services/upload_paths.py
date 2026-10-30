"""上传监听路径的校验。

**没有白名单**——监听目录完全由用户在面板上配置（跟随光鸭目标目录的
交互方式）。故这里只做**必要的**校验：

1. 必须是**绝对路径**（相对路径没有意义 —— 进程的工作目录会变）
2. 词法规范化（消掉 ``..`` / ``.`` / 重复斜杠）
3. 必须**已存在且是目录**（监听不存在的目录没意义）
4. 软链按 ``realpath`` 解析后使用 —— 避免软链指向别处时行为诡异

**软链逃逸防护在别处**：:mod:`app.services.upload_filter` 的
``should_skip`` 会拒绝「解析后逃出监听根」的路径，
:mod:`app.services.upload_cleanup` 的 ``is_safe_to_delete`` 同理。

> ⚠️ **已知风险**（用户明确选择不做白名单）：服务能读到容器内的
> ``/app/.env``（明文密钥）与 ``/app/data/*.db``。若把监听目录设成
> ``/app``，这些文件会被传到网盘。**只应监听确实需要同步的目录。**
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class PathGuardError(ValueError):
    """路径非法（不是绝对路径 / 不存在 / 不是目录）。"""


def resolve_watch_dir(watch_root: str) -> Path:
    """校验并规范化监听目录，返回最终的绝对路径。

    Args:
        watch_root: 监听目录（绝对路径）。

    Raises:
        PathGuardError: 不是绝对路径 / 目录不存在 / 不是目录。
    """
    root = Path(watch_root)
    if not root.is_absolute():
        raise PathGuardError(f"监听目录必须是绝对路径：{watch_root}")
    target = Path(_normalize(root))

    if not target.is_dir():
        raise PathGuardError(f"监听目录不存在或不是目录：{target}")

    return target


def list_subdirs(path: Path) -> list[str]:
    """列出目录下的**子目录**（返回**完整路径**）。

    隐藏目录以 ``.`` 开头的一律不列 —— 它们多半是系统目录
    （``.git`` / ``.cache``），列出来只会让人误点。

    **返回完整路径而不是目录名**：前端把它当作「下一层的路径」直接
    请求。只返回目录名的话，前端会请求 ``path=mnt``（缺前导 ``/``）
    → 400「必须是绝对路径」→ 目录浏览卡在第一层。

    读不到就返回空列表（不抛 —— 面板上显示「无法读取」即可）。
    """
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        logger.warning("读取目录失败 %s：%s", path, exc)
        return []
    base = Path(_normalize(path))
    return sorted(
        str(base / entry.name)
        for entry in entries
        if entry.is_dir() and not entry.name.startswith(".")
    )


def _normalize(path: Path) -> str:
    """词法规范化（不碰磁盘）：消掉 ``..`` / ``.`` / 重复斜杠。"""
    parts: list[str] = []
    for part in Path(path).parts:
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return str(Path(*parts)) if parts else "/"


__all__ = [
    "PathGuardError",
    "list_subdirs",
    "resolve_watch_dir",
]
