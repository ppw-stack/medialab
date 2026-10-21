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
from datetime import datetime, timedelta
from typing import Any, Final

from app.adapters.sites.base import FeedItem
from app.core.timeutils import bj_now_dt, format_bj
from app.core.windows import FreeWindow
from app.domain.enums import FreeSource, FreeType

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

#: 退还/返还/偿还 关键字（springsunday 等站用不同文字表示"上传者已撤种"）
_REFUND_KEYWORDS: Final[frozenset[str]] = frozenset({"refund", "退还", "偿还", "返还"})


def is_refund_marker(text: str | None) -> bool:
    """检测文案/class 是否含"退还/返还"关键字。

    用于 window_parser 主流程优先判断：refund 优先于 free/twoupfree 等
    普通免费标记（退还种可能也含 free class）。
    """
    if not text:
        return False
    return any(kw in text.lower() for kw in _REFUND_KEYWORDS)


#: NexusPHP 免费标记 class → (FreeType, default_discount_pct)
_FREE_CLASS_MAP: Final[dict[str, tuple[str, int | None]]] = {
    "free": (FreeType.UNKNOWN, None),  # 需配合剩余时间判断
    "twoupfree": (FreeType.TWOU_TIMED, None),
    "twoup": (FreeType.TWOU_TIMED, None),
    "halfdown": (FreeType.NEUTRAL, 50),   # 50% 下载折扣
    "twouphalfdown": (FreeType.NEUTRAL, 50),
    "thirtypercent": (FreeType.NEUTRAL, 30),  # 30% 下载折扣
    "nodead": (FreeType.UNKNOWN, None),
    "highlight": (FreeType.UNKNOWN, None),
}

#: HHan 主题促销 class → (FreeType, default_discount_pct)
_PROMO_CLASS_MAP: Final[dict[str, tuple[str, int | None]]] = {
    "free": (FreeType.UNKNOWN, None),  # 需配合剩余时间判断
    "2xfree": (FreeType.TWOU_TIMED, None),
    "50": (FreeType.NEUTRAL, 50),  # 50% 下载折扣（promotion-tag-50）
    "30": (FreeType.NEUTRAL, 30),  # 30% 下载折扣（promotion-tag-30）
}

#: 可见文本里"X%" → 折扣百分比。覆盖 class 名（更精确，能识别 class 之外的值）
_TEXT_PCT_RE: Final = re.compile(r"(\d{1,3})\s*%")


def _promo_text_pct(promo_texts: list[str] | None) -> int | None:
    """从促销标签可见文本里提取折扣百分比。

    输入形如 ``["30%", "50%"]``（各站 parser 从促销标签收集）。取**第一个**
    可识别的百分比；无任何可识别值返回 ``None``（调用方回退到 class 推断的
    默认折扣）。

    只接受 ``0 < pct < 100``——100% 属永久免费（由 class 标记判定，不走
    折扣路径），0% 无意义。
    """
    if not promo_texts:
        return None
    for text in promo_texts:
        m = _TEXT_PCT_RE.search(str(text))
        if not m:
            continue
        try:
            pct = int(m.group(1))
        except ValueError:
            continue
        if 0 < pct < 100:
            return pct
    return None


def _parse_iso_dt(value: object) -> str | None:
    """Unix 秒 / 毫秒 / ISO 8601 / 数字字符串 → 北京时间字符串。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        # 兼容秒级与毫秒级 Unix timestamp：
        # 秒级 1e9(2001) ~ 4.1e9(2100)；毫秒级 1e12(2001) ~ 4.1e12(2100)
        if 1_000_000_000 < ts < 4_102_444_800:
            from datetime import UTC

            from app.core.timeutils import format_bj

            return format_bj(datetime.fromtimestamp(ts, UTC))
        if 1_000_000_000_000 < ts < 4_102_444_800_000:
            from datetime import UTC

            from app.core.timeutils import format_bj

            return format_bj(datetime.fromtimestamp(ts / 1000, UTC))
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
        from app.core.timeutils import format_bj, to_bj

        if dt.tzinfo is not None:
            return format_bj(dt)
        # 无时区标记：naive 视为北京时间（astimezone 会把 naive 当系统本地时区，
        # 容器 TZ=UTC 时产生 ±8h 偏移，必须用 to_bj 显式按北京时区解释）
        return to_bj(dt)
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


def _parse_abs_datetime(text: str | None) -> str | None:
    """从 "2026-10-11 11:27:07" 格式返回北京时间字符串（原样）。"""
    if not text:
        return None
    try:
        datetime.strptime(text.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            datetime.fromisoformat(text.strip())
        except ValueError:
            return None
    return text.strip()


def _classify_from_html(
    free_classes: list[str],
    remaining_text: str | None = None,
    *,
    promo_texts: list[str] | None = None,
) -> tuple[str, timedelta | None, float, int | None]:
    """从免费标记 class + 剩余时间文案提取免费窗口（NexusPHP 通用）。

    ``promo_texts``：促销标签的可见文本（如 ``["30%", "50%"]``），
    用于覆盖 class 推断的 discount_pct（更精确）。
    返回 ``(free_type, remaining_delta, confidence, discount_pct)``。
    """
    combined = " ".join(free_classes).lower() + " " + (remaining_text or "").lower()

    if any(m in combined for m in _PERMANENT_MARKERS):
        return FreeType.PERMANENT, None, 1.0, 100

    # 促销标签文本优先：class 推断不出折扣时，用可见文案定 NEUTRAL 折扣
    promo_pct = _promo_text_pct(promo_texts)

    has_free = any(c.lower() == "free" for c in free_classes)
    if has_free:
        delta = _parse_remaining_delta(remaining_text)
        if delta is not None:
            return FreeType.TIMED, delta, 0.95, None
        return FreeType.PERMANENT, None, 0.85, 100

    for cls in free_classes:
        cls_lower = cls.lower()
        mapped = _FREE_CLASS_MAP.get(cls_lower)
        if mapped is None:
            continue
        free_type, default_pct = mapped
        if free_type == FreeType.UNKNOWN:
            continue
        delta = _parse_remaining_delta(remaining_text)
        pct = default_pct if promo_pct is None else promo_pct
        if delta is not None and free_type != FreeType.NEUTRAL:
            return free_type, delta, 0.9, pct
        return free_type, None, 0.85, pct

    # 文本兜底：在剩余文案里识别 "X%" 折扣（class 没匹配到时）
    if promo_pct is not None:
        delta = _parse_remaining_delta(remaining_text)
        if delta is not None:
            return FreeType.TIMED, delta, 0.7, promo_pct
        return FreeType.NEUTRAL, None, 0.6, promo_pct

    if remaining_text:
        m = _TEXT_PCT_RE.search(remaining_text)
        if m:
            try:
                pct = int(m.group(1))
            except ValueError:
                pct = None
            if pct is not None and 0 < pct < 100:
                delta = _parse_remaining_delta(remaining_text)
                if delta is not None:
                    return FreeType.TIMED, delta, 0.7, pct
                return FreeType.NEUTRAL, None, 0.6, pct

    return FreeType.UNKNOWN, None, 0.0, None


def _classify_from_status(status: dict[str, Any]) -> tuple[str, str | None, int | None]:
    """从 m-team search API 的 status 块提取免费窗口。

    返回 ``(free_type, end_at, discount_pct)``。``discount_pct`` 仅
    NEUTRAL 时为 0–100；PERMANENT 固定 100；其他为 None。
    """
    # 1) 单种特别免费
    mall = status.get("mallSingleFree")
    if isinstance(mall, dict) and mall.get("status") == "ONGOING":
        return FreeType.TIMED, _parse_iso_dt(mall.get("endDate")), None

    # 2) 全站促销
    promo = status.get("promotionRule")
    if isinstance(promo, dict):
        disc = promo.get("discount", "NORMAL")
        end = _parse_iso_dt(promo.get("endTime"))
        # discount 语义：百分比（0-100），100 = 100% off = 免费，0 = 0% off = 无折扣
        # 之前 `disc == 0` 把 0% off 误判为免费，会把"无促销"当"全站免费"
        free_disc = disc == "FREE" or disc == 100 or disc == "100"
        if free_disc and end is not None:
            # 全站限时免费：有结束时间必须判 TIMED，不能当永久
            return FreeType.TIMED, end, None
        if disc == "FREE" or disc == 100 or disc == "100":
            return FreeType.PERMANENT, None, 100
        if end is not None:
            return FreeType.TIMED, end, None

    # 3) 标准 discount 字段
    disc_raw = status.get("discount")
    if isinstance(disc_raw, str):
        if disc_raw == "FREE":
            end = _parse_iso_dt(status.get("discountEndTime"))
            if end is not None:
                return FreeType.TIMED, end, None
            return FreeType.PERMANENT, None, 100
        if disc_raw.upper().startswith("PERCENT_"):
            try:
                pct = int(disc_raw.split("_", 1)[1])
            except (IndexError, ValueError):
                pct = 0
            if pct <= 0:
                return FreeType.PERMANENT, None, 100
            if 1 <= pct <= 75:
                return FreeType.NEUTRAL, None, pct
            if pct >= 100:
                return FreeType.PERMANENT, None, 100
        disc_val = 0
    else:
        try:
            disc_val = int(disc_raw or 0)
        except (TypeError, ValueError):
            disc_val = 0

    if disc_val >= 100:
        return FreeType.PERMANENT, None, 100
    if disc_val < 0 and disc_val >= -75:
        return FreeType.NEUTRAL, None, abs(disc_val)
    if disc_val < -75:
        return FreeType.TWOU_TIMED, None, None

    end = _parse_iso_dt(status.get("discountEndTime"))
    if end is not None:
        return FreeType.TIMED, end, None
    return FreeType.UNKNOWN, None, None


def _parse_dict(item: FeedItem) -> FreeWindow | None:
    """dict 路径：m-team API 站点的 free_payload。"""
    payload = item.free_payload
    if not payload or "status" not in payload:
        return None
    status = payload["status"]
    if not isinstance(status, dict):
        return None
    free_type, end_at, discount_pct = _classify_from_status(status)
    if free_type == FreeType.PERMANENT:
        return FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=None,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=1.0,
            discount_pct=discount_pct,
        )
    if end_at is not None:
        return FreeWindow(
            free_type=free_type,
            free_end_at=end_at,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=0.95,
            discount_pct=discount_pct,
        )
    return FreeWindow(
        free_type=free_type,
        free_end_at=None,
        free_source=FreeSource.SITE_DEFAULT,
        raw_text=_stringify_safe(payload),
        confidence=0.3,
        discount_pct=discount_pct,
    )


def _parse_nexushp_html(item: FeedItem) -> FreeWindow | None:
    """HTML scrape 路径：NexusPHP free_payload。"""
    payload = item.free_payload
    if not payload:
        return None
    if ("free_classes" not in payload
            and "remaining_text" not in payload
            and "remaining_abs" not in payload):
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
    promo_texts_raw = payload.get("promo_texts", [])
    promo_texts = (
        [str(t) for t in promo_texts_raw] if isinstance(promo_texts_raw, list) else []
    )

    # 退还/返还种优先识别：class 含 refund 关键字 或 剩余时间文案含 refund
    if "refund" in free_classes or is_refund_marker(remaining_text):
        refund_end_at: str | None = None
        if remaining_abs:
            refund_end_at = _parse_abs_datetime(remaining_abs)
        return FreeWindow(
            free_type=FreeType.REFUND,
            free_end_at=refund_end_at,
            free_source="site_default",
            confidence=0.95,
            discount_pct=None,
        )

    free_type, remaining_delta, confidence, discount_pct = _classify_from_html(
        free_classes, remaining_text, promo_texts=promo_texts
    )

    end_at: str | None = None
    if remaining_abs:
        end_at = _parse_abs_datetime(remaining_abs)
    if end_at is None and remaining_delta is not None:
        end_at = format_bj(bj_now_dt() + remaining_delta)

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
        # 降级为限时（PERMANENT 改判 TIMED）：不再视作 100% 免费
        effective_pct = None if free_type == FreeType.PERMANENT else discount_pct
        return FreeWindow(
            free_type=FreeType.TIMED,
            free_end_at=end_at,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=0.95,
            discount_pct=effective_pct,
        )
    if free_type == FreeType.PERMANENT:
        return FreeWindow(
            free_type=FreeType.PERMANENT,
            free_end_at=None,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=confidence,
            discount_pct=discount_pct,
        )
    if end_at is not None:
        return FreeWindow(
            free_type=free_type,
            free_end_at=end_at,
            free_source=FreeSource.API,
            raw_text=_stringify_safe(payload),
            confidence=confidence,
            discount_pct=discount_pct,
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
            discount_pct=discount_pct,
        )
    return FreeWindow(
        free_type=FreeType.UNKNOWN,
        free_end_at=None,
        free_source=FreeSource.SITE_DEFAULT,
        raw_text=_stringify_safe(payload),
        confidence=confidence,
        discount_pct=discount_pct,
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
            end = format_bj(bj_now_dt() + delta)
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
        item = FeedItem(title="[payload]", free_payload=payload)
        return WindowParser.parse(item)


#: 暴露给测试的内部函数（避免破坏旧测试覆盖）
__test_exports__ = [
    "_classify_from_status",
    "_classify_from_html",
    "_parse_remaining_delta",
    "_parse_abs_datetime",
]
