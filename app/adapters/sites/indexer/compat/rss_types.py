"""RSS 配方类型（移植自 MoviePilot ``app.application.rss``）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class RssLinkRecipe:
    """RSS 生成页面的地址、选择器与表单参数。"""

    url: str
    xpath: str
    params: dict[str, Any] = field(default_factory=dict)
    render: bool = False
    method: Literal["get", "post"] = "post"
