"""免费窗口解析器（业务层）。

从 :class:`~app.adapters.sites.base.FeedItem` 提取免费窗口信息并组装为
:class:`~app.core.windows.FreeWindow`。

原 SiteAdapter.parse_window 接口已移除——窗口解析是**业务关注点**而非站点差异
（站点适配层只负责采集原始信号：免费标记 class、剩余时间文案、API 状态字段），
因此由本服务统一收口，支持 4 种子解析策略：

1. **DictPayloadParser**：m-team 等 API 站点 — 从 status dict 提取 discount / discountEndTime
2. **PromoTagParser**：NexusPHP HHan 主题 — 从 ``promotion-tag-XXX`` 提取
3. **ProImgParser**：NexusPHP 标准主题（HDSky/Ourbits）— 从 ``<img class="pro_XXX">`` 提取
4. **StrRegexParser**：纯文本兜底 — 解析"剩余 X 天 Y 小时"中文文案
5. **PermanentMarkerParser**：识别 permanent/forever 标记
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.adapters.sites.base import FeedItem
from app.core.timeutils import utcnow
from app.core.windows import FreeWindow
from app.db.models import FreeSource, FreeType

#: 永久免费标记（出现在 class 或文本中时视为 PERMANENT）
_PERMANENT_MARKERS: Final = (
    "permanent", "forever", "lifetime", "永久免费",
)

#: "剩余 X天Y小时" 文案正则（支持简体/繁体/英文）
_REMAINING_RE: Final = re.compile(
    r"(?:剩余|剩餘|剩|left|remaining)\s*"
    r"(?:(?P<d>\d+)\s*(?:天|日|d|days?)\s*)?"
    r"(?:(?P<h>\d+)\s*(?:小时|小時|時|时|h|hours?)\s*)?"
    r"(?:(?P<m>\d+)\s*(?:分|分鐘|m|min)\s*)?",
    re.IGNORECASE,
)

#: 纯时长文案（无 "剩余" 前缀）："2天3小时" / "5小时12分钟" / "30分钟"
#: 出现于 onmouseover tooltip 等场景
_DURATION_RE: Final = re.compile(
    r"(?:(?P<d>\d+)\s*(?:天|日))?\s*"
    r"(?:(?P<h>\d+)\s*(?:小时|小時|時|时))?\s*"
    r"(?:(?P<m>\d+)\s*(?:分|分鐘|分钟))?",
)

#: NexusPHP 免费标记 class → FreeType 映射（旧版 NexusPHP 兼容）
_FREE_CLASS_MAP: Final[dict[str, str]] = {
    "free": FreeType.UNKNOWN,  # 需配合剩余时间判断
    "twoupfree": FreeType.TWOU_TIMED,
    "twoup": FreeType.TWOU_TIMED,
    "halfdown": FreeType.NEUTRAL,
    "twouphalfdown": FreeType.NEUTRAL,
    "thirtypercent": FreeType.NEUTRAL,
    "nodead": FreeType.UNKNOWN,
    "highlight": FreeType.UNKNOWN,
}

#: HHan 主题促销 class → FreeType
_PROMO_CLASS_MAP: Final[dict[str, str]] = {
    "free": FreeType.UNKNOWN,  # 需配合剩余时间判断
    "2xfree": FreeType.TWOU_TIMED,
    "50": FreeType.NEUTRAL,  # 50% 下载折扣
    "30": FreeType.NEUTRAL,  # 30% 下载折扣
}


def _parse_iso_dt(value: object) -> datetime | None:
    """Unix 秒 / ISO 8601 / 数字字符串 → naive UTC datetime。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if 1_000_000_000 < ts < 4_102_444_800:
            return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_iso_dt(int(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
    return None


def _parse_remaining_delta(text: str | None) -> timedelta | None:
    """从"剩余 X天Y小时"或"2天3小时"文案解析为 timedelta。"""
    if not text:
        return None
    m = _REMAINING_RE.search(text)
    if not m:
        m = _DURATION_RE.search(text)
    if not m:
        return None
    days = int(m.group("d") or 0)
    hours = int(m.group("h") or 0)
    minutes = int(m.group("m") or 0)
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    if delta.total_seconds() <= 0:
        return None
    return delta


def _parse_abs_datetime(text: str | None) -> datetime | None:
    """从 "2026-10-11 11:27:07" 格式解析为 naive UTC datetime。"""
    if not text:
        return None
    try:
        dt = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(text.strip())
        except ValueError:
            return None
    return dt.replace(tzinfo=None)


def _classify_from_html(
    free_classes: list[str],
    remaining_text: str | None = None,
) -> tuple[str, timedelta | None, float]:
    """从免费标记 class + 剩余时间文案提取免费窗口（NexusPHP 通用）。"""
    combined = " ".join(free_classes).lower() + " " + (remaining_text or "").lower()

    if any(m in combined for m in _PERMANENT_MARKERS):
        return FreeType.PERMANENT, None, 1.0

    has_free = any(c.lower() == "free" for c in free_classes)
    if has_free:
        delta = _parse_remaining_delta(remaining_text)
        if delta is not None:
            return FreeType.TIMED, delta, 0.95
        return FreeType.PERMANENT, None, 0.85

    for cls in free_classes:
        cls_lower = cls.lower()
        mapped = _FREE_CLASS_MAP.get(cls_lower)
        if mapped and mapped != FreeType.UNKNOWN:
            delta = _parse_remaining_delta(remaining_text)
            if delta is not None and mapped != FreeType.NEUTRAL:
                return mapped, delta, 0.9
            return mapped, None, 0.85

    return FreeType.UNKNOWN, None, 0.0


def _classify_from_status(status: dict[str, Any]) -> tuple[str, datetime | None]:
    """从 m-team search API 的 status 块提取免费窗口。"""
    # 1) 单种特别免费
    mall = status.get("mallSingleFree")
    if isinstance(mall, dict) and mall.get("status") == "ONGOING":
        return FreeType.TIMED, _parse_iso_dt(mall.get("endDate"))

    # 2) 全站促销
    promo = status.get("promotionRule")
    if isinstance(promo, dict):
        disc = promo.get("discount", "NORMAL")
        end = _parse_iso_dt(promo.get("endTime"))
        if disc == "FREE" or disc == 100 or disc == "100":
            return FreeType.PERMANENT, None
        if end is not None:
            return FreeType.TIMED, end

    # 3) 标准 discount 字段
    disc_raw = status.get("discount")
    if isinstance(disc_raw, str):
        if disc_raw == "FREE":
            return FreeType.PERMANENT, None
        if disc_raw.upper().startswith("PERCENT_"):
            try:
                pct = int(disc_raw.split("_", 1)[1])
            except (IndexError, ValueError):
                pct = 0
            if pct <= 0:
                return FreeType.PERMANENT, None
            if 1 <= pct <= 75:
                return FreeType.NEUTRAL, None
            if pct >= 100:
                return FreeType.PERMANENT, None
        disc_val = 0
    else:
        try:
            disc_val = int(disc_raw or 0)
        except (TypeError, ValueError):
            disc_val = 0

    if disc_val >= 100:
        return FreeType.PERMANENT, None
    if disc_val < 0 and disc_val >= -75:
        return FreeType.NEUTRAL, None
    if disc_val < -75:
        return FreeType.TWOU_TIMED, None

    end = _parse_iso_dt(status.get("discountEndTime"))
    if end is not None:
        return FreeType.TIMED, end
    return FreeType.UNKNOWN, None


def _parse_dict(item: FeedItem) -> FreeWindow | None:
    """dict 路径：m-team API 站点的 free_payload。"""
    payload = item.free_payload
    if not payload or "status" not in payload:
        return None
    status = payload["status"]
    if not isinstance(status, dict):
        return None
    free_type, end_at = _classify_from_status(status)
    if free_type == FreeType.PERMANENT:
        return FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=None,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=1.0,
        )
    if end_at is not None:
        return FreeWindow(
            free_type=free_type,
            free_end_at=end_at,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=0.95,
        )
    return FreeWindow(
        free_type=free_type,
        free_end_at=None,
        free_source=FreeSource.SITE_DEFAULT,
        raw_text=_stringify_safe(payload),
        confidence=0.3,
    )


def _parse_nexushp_html(item: FeedItem) -> FreeWindow | None:
    """HTML scrape 路径：NexusPHP free_payload。"""
    payload = item.free_payload
    if not payload:
        return None
    if "free_classes" not in payload and "remaining_text" not in payload and "remaining_abs" not in payload:
        return None
    free_classes_raw = payload.get("free_classes", [])
    if isinstance(free_classes_raw, list):
        free_classes = [str(c) for c in free_classes_raw]
    elif isinstance(free_classes_raw, str):
        free_classes = [free_classes_raw]
    else:
        free_classes = []

    remaining_text_raw = payload.get("remaining_text")
    remaining_text = remaining_text_raw if isinstance(remaining_text_raw, str) else None
    remaining_abs_raw = payload.get("remaining_abs")
    remaining_abs = remaining_abs_raw if isinstance(remaining_abs_raw, str) else None

    free_type, remaining_delta, confidence = _classify_from_html(
        free_classes, remaining_text
    )

    end_at: datetime | None = None
    if remaining_abs:
        end_at = _parse_abs_datetime(remaining_abs)
    if end_at is None and remaining_delta is not None:
        end_at = utcnow() + remaining_delta

    # R10 契约：NEUTRAL / TWOU_TIMED 必须 free_end_at=None（中性/2X 无时间窗口）。
    # 若 user 同时给了 remaining_abs，按 free_type 优先：
    # - NEUTRAL：忽略 end_at（中性下载不限时）
    # - TWOU_TIMED：忽略 end_at（2X 永久有效）
    if free_type in (FreeType.NEUTRAL, FreeType.TWOU_TIMED):
        end_at = None

    if end_at is not None and free_type in (
        FreeType.PERMANENT,
        FreeType.TIMED,
        FreeType.UNKNOWN,
    ):
        return FreeWindow(
            free_type=FreeType.TIMED,
            free_end_at=end_at,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=0.95,
        )
    if free_type == FreeType.PERMANENT:
        return FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=None,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=confidence,
        )
    if end_at is not None:
        return FreeWindow(
            free_type=free_type,
            free_end_at=end_at,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=confidence,
        )
    if (
        free_type != FreeType.UNKNOWN
        and free_type in (FreeType.TWOU_TIMED, FreeType.NEUTRAL)
    ):
        return FreeWindow(
            free_type=free_type,
            free_end_at=None,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=confidence,
        )
    return FreeWindow(
        free_type=FreeType.UNKNOWN,
        free_end_at=None,
        free_source=FreeSource.SITE_DEFAULT,
        raw_text=_stringify_safe(payload),
        confidence=confidence,
    )


def _parse_str(item: FeedItem) -> FreeWindow:
    """str 路径：HTML 详情页或 RSS description 的"剩余 X"文案兜底。"""
    text = item.description or ""
    lowered = text.lower()
    if any(m in lowered for m in _PERMANENT_MARKERS):
        return FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=None,
            free_source=FreeSource.LIST_TEXT,
            raw_text=text,
            confidence=0.9,
        )
    m = _REMAINING_RE.search(text)
    if m:
        days = int(m.group("d") or 0)
        hours = int(m.group("h") or 0)
        minutes = int(m.group("m") or 0)
        delta = timedelta(days=days, hours=hours, minutes=minutes)
        if delta.total_seconds() > 0:
            end = utcnow() + delta
            return FreeWindow(
                free_type=FreeType.TIMED,
                free_end_at=end,
                free_source=FreeSource.LIST_TEXT,
                raw_text=text,
                confidence=0.8,
            )
    return FreeWindow(
        free_type=FreeType.UNKNOWN,
        free_end_at=None,
        free_source=FreeSource.SITE_DEFAULT,
        raw_text=text,
        confidence=0.0,
    )


def _stringify_safe(raw: dict[str, Any]) -> str:
    parts: list[str] = []
    for k in (
        "id", "title", "name", "smallDescr", "discount", "discountEndTime",
        "mallSingleFree", "promotionRule", "free_classes", "remaining_text",
        "remaining_abs", "status",
    ):
        if k in raw and raw[k] is not None:
            parts.append(f"{k}={raw[k]}")
    return " ".join(parts)[:2000]


class WindowParser:
    """FeedItem → FreeWindow 解析器入口。"""

    @staticmethod
    def parse(item: FeedItem) -> FreeWindow:
        """根据 free_payload / description 自动选择解析策略。"""
        if item.free_payload:
            parsed = _parse_dict(item)
            if parsed is not None:
                return parsed
            parsed = _parse_nexushp_html(item)
            if parsed is not None:
                return parsed
        return _parse_str(item)

    @staticmethod
    def parse_payload(payload: dict[str, object]) -> FreeWindow:
        """直接接受 payload dict（测试便捷入口）。"""
        item = FeedItem(title="[payload]", link="", free_payload=payload)
        return WindowParser.parse(item)


#: 暴露给测试的内部函数（避免破坏旧测试覆盖）
__test_exports__ = [
    "_classify_from_status",
    "_classify_from_html",
    "_parse_remaining_delta",
    "_parse_abs_datetime",
]
