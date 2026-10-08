"""配置驱动的通用站点适配器（FR-1.5.1 / FR-1.5.2 / FR-1.5.3）。

绝大多数 PT 站点共用自研 PHP + 相似 URL 结构，因此用配置表覆盖差异，
新增站点通常**无需写代码**（AC-16）。站点专有能力再以子类覆盖。

``parse_window`` 是全系统最敏感的函数，实现遵循附录 D 的全部用例。
"""

from __future__ import annotations

import email.utils
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFeature,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
)
from app.core.logging import get_logger
from app.core.timeutils import ClockOffset, parse_duration_seconds
from app.core.windows import FreeWindow
from app.db.models import FreeSource, FreeType

logger = get_logger(__name__)

#: 站点"剩余 X"文案默认正则：覆盖中/繁/英与多种分隔（附录 D 测试口径）
DEFAULT_TIME_PATTERN = (
    r"(?:剩余|剩|尚余|left|remaining)?\s*"
    r"(?:(?P<d>\d+(?:\.\d+)?)\s*(?:天|日|d|days?)\s*)?"
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:小时|小時|时|h|hours?|hrs?)\s*)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*(?:分钟|分鐘|分|m|min|mins?)\s*)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*(?:秒|s|sec|secs?)\s*)?"
)

#: 永久免费标记。**判定优先级最高**（R10）
PERMANENT_MARKERS: tuple[str, ...] = (
    "permanent",
    "永久免费",
    "永久免費",
    "无限期免费",
    "無期限",
    "free forever",
    "lifetime",
    "-1x",  # 常见于 discount=100 的展示
)

#: 限时免费标记
TIMED_MARKERS: tuple[str, ...] = ("限时免费", "限時免費", "限时", "限时促销", "timed")
#: 2X 标记
TWOU_MARKERS: tuple[str, ...] = ("2x", "2倍", "double", "双倍")
#: 中性种标记
NEUTRAL_MARKERS: tuple[str, ...] = ("中性", "neutral", "0.5x", "半价")

#: 免费结束时刻字段候选（FR-3.0 来源优先级 1）
END_FIELD_CANDIDATES: tuple[str, ...] = (
    "free_until",
    "promotion_end",
    "expires_at",
    "expire_at",
    "free_end",
    "until",
)


class GenericSiteAdapter(SiteAdapter):
    """配置驱动的通用站点适配器。"""

    adapter_type = "generic"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        cookies: str | None = None,
        timeout: float = 20.0,
        clock_offset: ClockOffset | None = None,
    ) -> None:
        super().__init__(profile)
        self._cookies = cookies
        self._timeout = timeout
        self._clock_offset = clock_offset or ClockOffset.zero()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            cookies=self._parse_cookies(cookies),
            headers={"User-Agent": "SeedHelper/0.1 (+https://example.invalid/seedhelper)"},
        )
        self._pattern = self._compile_pattern()

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _parse_cookies(raw: str | None) -> dict[str, str]:
        if not raw:
            return {}
        out: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _compile_pattern(self) -> re.Pattern[str] | None:
        raw = self.profile.rss_time_pattern
        if not raw:
            return None
        try:
            return re.compile(raw, re.IGNORECASE)
        except re.error as exc:
            logger.error("站点 %s 的 rss_time_pattern 非法，已忽略：%s", self.name, exc)
            return None

    def _time_pattern(self) -> re.Pattern[str] | None:
        return self._pattern

    # ------------------------------------------------------------ 接口
    async def ping(self) -> SiteHealth:
        try:
            resp = await self._client.get(f"https://{self.profile.domain}/", timeout=self._timeout)
        except httpx.HTTPError as exc:
            return SiteHealth(reachable=False, detail=str(exc))
        if resp.status_code >= 500:
            return SiteHealth(reachable=False, detail=f"HTTP {resp.status_code}")
        return SiteHealth(reachable=True, logged_in=self._has_session(resp))

    def _has_session(self, resp: httpx.Response) -> bool:
        """站点结构各异，此处保守判定：能取到页面且非登录跳转即视为已登录。"""
        ctype = resp.headers.get("content-type", "")
        return "text/html" in ctype and "login" not in resp.url.path.lower()

    async def fetch_feed(self, url: str) -> list[FeedItem]:
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("站点 %s RSS 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code in {403, 429}:
            raise _BreakerTripped(f"站点 {self.name} 返回 {resp.status_code}，需熔断（FR-2.2）")
        if resp.status_code >= 400:
            logger.warning("站点 %s RSS 返回 %s", self.name, resp.status_code)
            return []
        return self.parse_feed(resp.text)

    def parse_feed(self, xml: str) -> list[FeedItem]:
        """RSS 解析（FR-1.5.1 归一化）。

        使用 ``xml.etree`` 容错解析，避免为每个站点引入专用解析器。
        """
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(xml.encode("utf-8"), parser=_safe_parser())
        except ET.ParseError as exc:
            logger.warning("站点 %s RSS 解析失败：%s", self.name, exc)
            return []

        items: list[FeedItem] = []
        for node in root.iter():
            if not node.tag.endswith("item") and not node.tag.endswith("entry"):
                continue
            fields = {_localname(c.tag): (c.text or "").strip() for c in node}
            link = fields.get("link") or fields.get("guid") or ""
            if not link:
                continue
            items.append(
                FeedItem(
                    title=fields.get("title", ""),
                    link=link,
                    size=_parse_size(fields.get("size") or fields.get("contentlength") or ""),
                    category=fields.get("category") or None,
                    description=fields.get("description", ""),
                    torrent_url=fields.get("link") or None,
                )
            )
        return items

    # -------------------------------------------------------- parse_window
    async def parse_window(
        self, raw: str | dict[str, object], *, source_hint: str | None = None
    ) -> FreeWindow:
        """FR-1.5.3 契约实现。

        判定顺序严格如下（顺序本身是需求的一部分）：

        1. **永久免费优先**（R10）：命中永久标记 → ``free_end_at=None``
        2. 结构化字段（来源 1，``api``）：置信度 0.95
        3. 文案解析（来源 2/3）。``source_hint`` 显式指定文案来源，
           未指定时按入参形态推断：``dict`` → ``list_text``，``str`` → ``rss_desc``
        4. 均失败 → ``unknown`` + 低置信度，由业务层兜底

        ``source_hint`` 供调用方（列表页 vs RSS 描述）消除歧义。
        """
        text = raw if isinstance(raw, str) else _stringify(raw)

        # 1) 永久免费：优先级最高，任何来源都不得推翻
        if _contains_marker(text, PERMANENT_MARKERS):
            return FreeWindow(
                free_type=FreeType.PERMANENT,
                free_end_at=None,
                free_source=FreeSource.API,
                raw_text=text,
                confidence=1.0,
            )

        # 2) 结构化时间字段（来源优先级 1：最准确）
        if isinstance(raw, dict):
            field_name = self._pick_end_field(raw)
            if field_name:
                parsed = self._parse_end_value(raw[field_name])
                if parsed is not None:
                    return FreeWindow(
                        free_type=self._infer_type(text, raw),
                        free_end_at=parsed,
                        free_source=FreeSource.API,
                        raw_text=text,
                        confidence=0.95,
                    )

        # 3) 文案解析（列表页"剩余 X" 或 RSS 描述）
        duration = self.parse_remaining_duration(text)
        if duration is not None:
            end = self._clock_offset.remaining_to_absolute(duration)
            return FreeWindow(
                free_type=self._infer_type(text, raw),
                free_end_at=end,
                free_source=source_hint
                or (FreeSource.LIST_TEXT if isinstance(raw, dict) else FreeSource.RSS_DESC),
                raw_text=text,
                confidence=0.8,
            )

        # 4) 无法判定：保守交由业务层兜底
        return FreeWindow(
            free_type=self._infer_type(text, raw),
            free_end_at=None,
            free_source=FreeSource.SITE_DEFAULT,
            raw_text=text,
            confidence=0.0,
        )

    def parse_remaining_duration(self, text: str) -> timedelta | None:
        """从文案解析"剩余时长"（FR-1.5.3 / 附录 D）。

        返回 ``None`` 表示文案不含可解析的剩余时长。永久免费文案显式返回
        ``None``（由调用方先判永久标记）。
        """
        pattern = self._time_pattern()
        if pattern is None:
            return None
        m = pattern.search(text)
        if not m:
            return None
        groups = m.groupdict()
        try:
            days = float(groups.get("d") or 0)
            hours = float(groups.get("h") or 0)
            minutes = float(groups.get("m") or 0)
            seconds = float(groups.get("s") or 0)
        except (TypeError, ValueError):
            return None
        total = days * 86400 + hours * 3600 + minutes * 60 + seconds
        if total <= 0:
            return None
        return timedelta(seconds=total * max(self.profile.time_unit_multiplier, 1))

    def _pick_end_field(self, raw: dict[str, object]) -> str | None:
        """按 ``window_field_map`` → 候选列表的顺序选字段。"""
        configured = self.profile.window_field_map or {}
        preferred = configured.get("end") if isinstance(configured, dict) else None
        names: list[str] = []
        if isinstance(preferred, str):
            names.append(preferred)
        if isinstance(preferred, list):
            names.extend(str(n) for n in preferred)
        alt = configured.get("alt") if isinstance(configured, dict) else None
        if isinstance(alt, list):
            names.extend(str(n) for n in alt)
        names.extend(END_FIELD_CANDIDATES)
        for name in names:
            if name in raw and raw[name] not in (None, "", 0, "0"):
                return name
        return None

    def _parse_end_value(self, value: object) -> datetime | None:
        """结束时刻解析：Unix 秒 / ISO 字符串 / "剩余 X" 文案。"""
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            if value <= 0:
                return None
            # 合理区间过滤：1970~2100，防止站点返回异常值
            if value < 1_000_000_000 or value > 4_102_444_800:
                return None
            return datetime.fromtimestamp(float(value), UTC).replace(tzinfo=None)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value
            return value.astimezone(UTC).replace(tzinfo=None)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return self._parse_end_value(int(text))
            # ISO 8601
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                pass
            else:
                return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
            dur = self.parse_remaining_duration(text)
            if dur is not None:
                return self._clock_offset.remaining_to_absolute(dur)
            try:
                dur2 = parse_duration_seconds(text)
            except ValueError:
                dur2 = None
            if dur2:
                return self._clock_offset.remaining_to_absolute(dur2)
        return None

    def _infer_type(self, text: str, raw: object) -> str:
        """按标记与配置推导 ``free_type``。"""
        # 下载因子映射优先（download_factor_map）
        if isinstance(raw, dict):
            factor = raw.get("download_factor") or raw.get("discount")
            if factor is not None:
                mapped = self.profile.download_factor_map.get(str(factor))
                if mapped:
                    return mapped
        mapped_type = self.profile.free_type_map
        lowered = text.lower()
        for key, value in mapped_type.items():
            if key.lower() in lowered:
                return value
        if _contains_marker(text, TWOU_MARKERS):
            return FreeType.TWOU_TIMED
        if _contains_marker(text, NEUTRAL_MARKERS):
            return FreeType.NEUTRAL
        if _contains_marker(text, TIMED_MARKERS):
            return FreeType.TIMED
        if isinstance(raw, dict) and raw.get("promotion") in {"freeleech", "free"}:
            return FreeType.TIMED
        return FreeType.UNKNOWN

    # ------------------------------------------------------------ 其他
    async def torrent_detail(self, item_id: str) -> dict[str, object]:
        url = self.profile.detail_url(id=item_id, hash=item_id)
        if not url:
            return {}
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError:
            return {}
        if resp.status_code >= 400:
            return {}
        import json

        try:
            data = json.loads(resp.text)
        except ValueError:
            return {"_html": resp.text[:20000]}
        return data if isinstance(data, dict) else {}

    async def verify_status(self, info_hash: str) -> TorrentBrief:
        if SiteFeature.STATUS_VERIFY not in self.capabilities():
            return TorrentBrief.UNKNOWN
        detail = await self.torrent_detail(info_hash)
        if not detail:
            return TorrentBrief.UNKNOWN
        if detail.get("deleted") or detail.get("is_deleted"):
            return TorrentBrief.DELETED
        return TorrentBrief.EXISTS

    async def site_clock(self) -> datetime | None:
        """探测站点服务器时间（FR-3.0.2）。

        优先解析 ``Date`` 响应头（RFC 7231，秒级精度对时钟校正足够）；
        无该头时退化为 ``/`` 响应或判为不支持，由业务层沿用手动校正值。
        """
        if SiteFeature.CLOCK_API not in self.capabilities():
            return None
        try:
            resp = await self._client.head(
                f"https://{self.profile.domain}/", timeout=self._timeout
            )
        except httpx.HTTPError:
            return None
        raw_date = resp.headers.get("date")
        if not raw_date:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(UTC).replace(tzinfo=None)

    def capabilities(self) -> frozenset[SiteFeature]:
        return frozenset({SiteFeature.RSS})


class _BreakerTripped(RuntimeError):
    """站点熔断（FR-2.2：429/403 立即熔断）。"""


# ---------------------------------------------------------------- 辅助
def _safe_parser() -> Any:
    import xml.etree.ElementTree as ET

    parser = ET.XMLParser()
    return parser


def _localname(tag: str) -> str:
    """去掉 XML 命名空间（``{ns}title`` → ``title``）。"""
    return tag.rsplit("}", 1)[-1].lower()


def _stringify(raw: dict[str, object]) -> str:
    parts: list[str] = []
    for key, value in raw.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _parse_size(text: str) -> int:
    """解析 ``1.5 GB`` / ``536870912``。"""
    raw = text.strip()
    if not raw:
        return 0
    match = re.match(r"([\d.]+)\s*([KMGT]?B?)(?:i?B)?", raw, re.IGNORECASE)
    if not match:
        return 0
    try:
        num = float(match.group(1))
    except ValueError:
        return 0
    mult = {
        "": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }.get(match.group(2).upper(), 1)
    return int(num * mult)


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(m.lower() in lowered for m in markers)