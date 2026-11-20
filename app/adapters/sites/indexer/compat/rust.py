"""Rust 加速层的空实现。

MoviePilot 用 ``moviepilot_rust`` 的索引解析器做加速；本项目不引入该原生
依赖，返回 ``None`` 让 :class:`SiteSpider` 走 Python 解析路径。
"""

from __future__ import annotations

from typing import Any


def parse_indexer_torrents(**_kwargs: Any) -> None:
    """Rust 解析入口占位：始终回退 Python。"""
    return None


def parse_indexer_subtitles(**_kwargs: Any) -> None:
    """Rust 字幕解析入口占位：始终回退 Python。"""
    return None
