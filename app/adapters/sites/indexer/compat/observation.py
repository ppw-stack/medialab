"""站点搜索观测的占位实现（本项目未接该观测链）。"""

from __future__ import annotations

from typing import Any


def report_site_search_page(**_kwargs: Any) -> None:
    """记录搜索分页事实；本项目暂不采集。"""
    return None


def report_site_search_outcome(**_kwargs: Any) -> None:
    """记录搜索结果事实；本项目暂不采集。"""
    return None
