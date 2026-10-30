"""STRM 文件写出。

**幂等**：目标文件已存在且内容相同 → 跳过，不重写。
理由：媒体服务器会监控 strm 文件的 mtime，无谓重写会导致它反复重扫元数据。
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class StrmWriteResult(str):
    """写出结果：``created`` / ``unchanged`` / ``updated``。"""

    CREATED = "created"
    UNCHANGED = "unchanged"
    UPDATED = "updated"


def write_strm(target: Path, url: str) -> str:
    """写 STRM 文件（自动建父目录）。

    Args:
        target: 目标路径。
        url: 中转 URL（写进文件内容）。

    Returns:
        ``created`` / ``unchanged`` / ``updated``。
    """
    content = url + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == content:
            return StrmWriteResult.UNCHANGED
        target.write_text(content, encoding="utf-8")
        return StrmWriteResult.UPDATED

    target.write_text(content, encoding="utf-8")
    return StrmWriteResult.CREATED


def read_strm(target: Path) -> str | None:
    """读 STRM 内容（去掉尾部换行）。文件不存在返回 None。"""
    try:
        return target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def delete_strm(target: Path) -> bool:
    """删 STRM 文件。返回是否真的删了。"""
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("删除 STRM 失败 %s：%s", target, exc)
        return False
