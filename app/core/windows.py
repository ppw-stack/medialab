"""免费窗口解析契约（FR-1.5.3）。

这是全系统时效性核心，改动前请先读 FR-3.0 / FR-3.4。

不可协商的规则：**适配层只解析、不判定**（FR-1.5.3）。适配层产出
:class:`FreeWindow`，窗口到期后的动作由 :mod:`app.services.monitor`
（巡检 + 临近截止自动暂停）依据 ``torrent_index.free_end_at`` 判定。

历史：FR-3.0.1 / FR-3.4 的截止时间模型（``WindowPolicy`` /
``WindowDeadlines`` / ``abort_at``）与站点时钟偏差 ``ClockOffset`` 一起
下线——实际中止逻辑走 ``torrent_index.free_end_at``，见
:mod:`app.services.monitor`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.domain.enums import (
    NON_ABORTABLE_FREE_TYPES,
    TYPES_WITHOUT_WINDOW,
    FreeSource,
    FreeType,
)

#: 置信度低于此值按 unknown 保守处理（FR-1.5.3）
LOW_CONFIDENCE_THRESHOLD: Final = 0.6


@dataclass(frozen=True, slots=True)
class FreeWindow:
    """站点适配层解析出的窗口信息（FR-1.5.3 契约）。"""

    free_type: str = FreeType.UNKNOWN
    free_end_at: str | None = None
    free_source: str = FreeSource.SITE_DEFAULT
    raw_text: str = ""
    confidence: float = 0.0
    #: 具体下载折扣百分比（0–100）。仅当 free_type=NEUTRAL 时才有意义
    #: （50 = 半价下载、30 = 三折下载）；永久免费固定 100；其他为 None。
    discount_pct: int | None = None

    @property
    def is_permanent(self) -> bool:
        """是否永久免费（不参与超时中止，FR-3.0 表、R10）。

        注意与「无窗口类型」区分：NEUTRAL（半价下载）**不是**永久免费——
        monitor 的规则是「当前不是免费种 → 立即暂停」，NEUTRAL 走这条路径。
        """
        return self.free_type in NON_ABORTABLE_FREE_TYPES

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD

    @property
    def is_actionable(self) -> bool:
        """是否需要挂看护。永久免费与低置信度均不挂。"""
        return (
            self.free_end_at is not None
            and not self.is_permanent
            and not self.is_low_confidence
        )

    def __post_init__(self) -> None:
        # 无窗口类型不得带结束时刻，避免误入看护链路。
        # 与「是否永久免费」是两个概念——见 app.db.models.TYPES_WITHOUT_WINDOW
        if self.free_type in TYPES_WITHOUT_WINDOW and self.free_end_at is not None:
            raise ValueError(
                f"free_type={self.free_type} 为非限时类型，free_end_at 必须为 None（R10）"
            )
