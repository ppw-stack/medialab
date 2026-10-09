"""m-team 站点适配器（FR-1.5）。

m-team 的 RSS 是 JSON API（不是 XML），端点：
    GET https://<domain>/api/rss/fetch?uid=<u>&t=<ts>&sign=<s>&tkeys=<k>

响应格式（顶层）：
    {
        "code": 0,            # 0 = 成功
        "message": "success",
        "data": [ {<item>}, ... ]
    }

每条 item 关键字段（待联调核实，可能命名略不同）：
    id / tid / hash / infohash    # 种子 ID 与 InfoHash
    title / small_descr / name     # 标题与描述
    size                           # 字节
    category / classification       # 主分类
    link / download / url          # 详情页 / 下载 URL
    discount / free / free_deadline / free_until
                                    # 折扣 / 限免 / 限免截止
    status / seeders / leechers / completed

免费类型识别优先级（FR-1.5.3 顺序）：
1. discount == 100 → 永久免费（FreeType.PERMANENT）
2. discount in {0} → 限时免费（FreeType.TIMED）
3. discount in {50, -50} → 中性种（FreeType.NEUTRAL，2x 100% 算永久 + 2x）
4. discount > 0 且 < 100 → 限时免费（FreeType.TIMED）
5. discount > 100 → 2X（FreeType.TWOU_TIMED）

注意：仅 discount 字段不够（很多条目 discount=0 但 free 字段是 true），
       需结合 ``free`` 字段与 ``free_deadline`` 一并判断。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFeature,
    SiteHealth,
    TorrentBrief,
)
from app.core.logging import get_logger
from app.core.timeutils import ClockOffset, utcnow
from app.core.windows import FreeWindow
from app.db.models import FreeSource, FreeType

logger = get_logger(__name__)


#: 临时限制单次拉取的最大条目数（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: 详情页路径模板（保留为占位符；按需覆盖）
_DEFAULT_DETAIL_PATH = "/detail/{id}"


class MteamAdapter(SiteAdapter):
    """m-team 站点 JSON API 适配器。"""

    adapter_type = "mteam"

    def __init__(
        self,
        profile,
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

    def _base_url(self) -> str:
        return f"https://{self.profile.domain}"

    def _detail_url(self, item_id: str) -> str | None:
        if self.profile.detail_path_template:
            path = self.profile.detail_path_template.format(
                id=item_id, hash=item_id
            )
        else:
            path = _DEFAULT_DETAIL_PATH.format(id=item_id)
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url()}{path}"

    @staticmethod
    def _info_hash_from(item: dict[str, Any]) -> str | None:
        for key in ("infohash", "info_hash", "hash", "torrent_hash"):
            v = item.get(key)
            if isinstance(v, str) and len(v.strip()) == 40:
                return v.strip().lower()
        return None

    @staticmethod
    def _parse_iso_dt(value: object) -> datetime | None:
        """把 ISO 8601 / Unix 秒 / 数字字符串解析为 naive UTC datetime。"""
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
                return MteamAdapter._parse_iso_dt(int(text))
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
        return None

    @staticmethod
    def _classify_free(item: dict[str, Any]) -> tuple[str, datetime | None]:
        """根据 discount / free / free_deadline 字段返回 (free_type, end_at)。

        判定顺序：永久 > 限时 > 中性 > 2X > 未知。
        """
        # discount: m-team 的 "免费下载" 折扣（百分比），常规 0 = 付费、100 = 永久免费
        # 负数 = 收费乘数（如 -50 表示 1/0.5 = 2x 上传）
        discount = item.get("discount")
        free_flag = item.get("free")  # 布尔或数字
        deadline_raw = (
            item.get("free_deadline")
            or item.get("free_end")
            or item.get("free_until")
        )
        deadline = MteamAdapter._parse_iso_dt(deadline_raw)

        # 1) 永久免费
        if discount == 100 or discount == "100":
            return FreeType.PERMANENT, None
        if isinstance(free_flag, bool) and free_flag and deadline is None:
            # 标记为 free 但无截止 → 视作永久
            return FreeType.PERMANENT, None

        # 2) 限时免费
        if deadline is not None:
            # discount 0 是常规默认；有截止 = 限时
            return FreeType.TIMED, deadline

        # 3) 中性种：discount 接近 -50（约等于 2x 但仅 50% 下载）
        if isinstance(discount, (int, float)) and discount < 0 and discount >= -75:
            return FreeType.NEUTRAL, None

        # 4) 2X：discount 接近 -100
        if isinstance(discount, (int, float)) and discount < -75:
            return FreeType.TWOU_TIMED, None

        # 5) 默认：未知
        return FreeType.UNKNOWN, None

    # ------------------------------------------------------------ 接口实现
    async def ping(self) -> SiteHealth:
        try:
            resp = await self._client.get(f"{self._base_url()}/", timeout=self._timeout)
        except httpx.HTTPError as exc:
            return SiteHealth(reachable=False, detail=str(exc))
        if resp.status_code >= 500:
            return SiteHealth(reachable=False, detail=f"HTTP {resp.status_code}")
        return SiteHealth(
            reachable=True,
            logged_in="text/html" in resp.headers.get("content-type", ""),
            detail="",
        )

    async def fetch_feed(self, url: str) -> list[FeedItem]:
        """拉取 JSON API RSS 并归一化。"""
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("m-team %s RSS 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code in {403, 429}:
            raise _BreakerTripped(f"m-team {self.name} 返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            logger.warning("m-team %s RSS 返回 %s", self.name, resp.status_code)
            return []
        return self.parse_feed(resp.text)

    def parse_feed(self, raw_text: str) -> list[FeedItem]:
        """解析 m-team JSON RSS 响应。"""
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            logger.warning("m-team %s RSS 非 JSON：%s", self.name, exc)
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("code") not in (0, "0", None):
            logger.warning(
                "m-team %s RSS code=%s message=%s",
                self.name, payload.get("code"), payload.get("message"),
            )
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        items: list[FeedItem] = []
        for raw in data[:_MAX_ITEMS_PER_PULL]:
            if not isinstance(raw, dict):
                continue
            title = (
                raw.get("title")
                or raw.get("name")
                or raw.get("small_descr")
                or ""
            )
            link = (
                raw.get("link")
                or raw.get("url")
                or (raw.get("id") and f"https://{self.profile.domain}/detail/{raw['id']}")
                or ""
            )
            if not link:
                continue
            description = (
                raw.get("small_descr")
                or raw.get("description")
                or ""
            )
            if isinstance(description, str):
                description = description[:20000]
            items.append(
                FeedItem(
                    title=str(title),
                    link=str(link),
                    info_hash=self._info_hash_from(raw),
                    size=int(raw.get("size") or 0),
                    category=raw.get("category") or raw.get("classification") or None,
                    description=str(description),
                    published_at=self._parse_iso_dt(
                        raw.get("created_at") or raw.get("pub_date")
                    ),
                    torrent_url=raw.get("download") or raw.get("torrent_url") or link,
                )
            )
        return items

    async def parse_window(
        self,
        raw: str | dict[str, object],
        *,
        source_hint: str | None = None,
    ) -> FreeWindow:
        """FR-1.5.3：解析单条目的免费窗口。

        入参形态：
        - ``str``：是 RSS description 或列表页文案（仍走文案解析，但 m-team
          列表页实际返回的是 JSON，文案入口主要用于搜索结果页）
        - ``dict``：是 JSON API 单条目的完整 dict
        """
        # dict 路径：直接读结构化字段
        if isinstance(raw, dict):
            free_type, end_at = self._classify_free(raw)
            # permanent 必须 free_end_at=None（__post_init__ 会校验）
            if free_type == FreeType.PERMANENT:
                return FreeWindow(
                    free_type=FreeType.PERMANENT,
                    free_end_at=None,
                    free_source=FreeSource.API,
                    raw_text=_stringify_safe(raw),
                    confidence=1.0,
                )
            if end_at is not None:
                return FreeWindow(
                    free_type=free_type,
                    free_end_at=end_at,
                    free_source=FreeSource.API,
                    raw_text=_stringify_safe(raw),
                    confidence=0.95,
                )
            # 没有截止时刻：unknown，让业务层兜底
            return FreeWindow(
                free_type=free_type,
                free_end_at=None,
                free_source=FreeSource.SITE_DEFAULT,
                raw_text=_stringify_safe(raw),
                confidence=0.3,
            )

        # str 路径：旧版 HTML 列表页的兜底（保留"剩余 X" 正则）
        text = raw or ""
        lowered = text.lower()
        if any(m in lowered for m in ("永久免费", "free forever", "lifetime", "permanent")):
            return FreeWindow(
                free_type=FreeType.PERMANENT,
                free_end_at=None,
                free_source=FreeSource.LIST_TEXT,
                raw_text=text,
                confidence=0.9,
            )
        m = re.search(
            r"(?:剩余|剩|left|remaining)\s*"
            r"(?:(?P<d>\d+)\s*(?:天|日|d|days?)\s*)?"
            r"(?:(?P<h>\d+)\s*(?:小时|时|h|hours?)\s*)?"
            r"(?:(?P<m>\d+)\s*(?:分|m|min)\s*)?",
            text, re.IGNORECASE,
        )
        if m:
            days = int(m.group("d") or 0)
            hours = int(m.group("h") or 0)
            minutes = int(m.group("m") or 0)
            delta = timedelta(days=days, hours=hours, minutes=minutes)
            if delta.total_seconds() > 0:
                end = self._clock_offset.remaining_to_absolute(delta)
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

    async def torrent_detail(self, item_id: str) -> dict[str, object]:
        """拉取详情页/详情 API。m-team 详情页含完整 discount/free/free_deadline 字段。

        返回 dict 后，调用方（``FreeSeedHunter``）会再次调 ``parse_window`` 二次精化。
        """
        url = self._detail_url(item_id)
        if not url:
            return {}
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError:
            return {}
        if resp.status_code >= 400:
            return {}
        try:
            data = json.loads(resp.text)
        except ValueError:
            return {"_html": resp.text[:20000]}
        return data if isinstance(data, dict) else {}

    async def verify_status(self, info_hash: str) -> TorrentBrief:
        detail = await self.torrent_detail(info_hash)
        if not detail:
            return TorrentBrief.UNKNOWN
        if detail.get("deleted") or detail.get("is_deleted"):
            return TorrentBrief.DELETED
        if detail.get("banned") or detail.get("violation"):
            return TorrentBrief.VIOLATION
        return TorrentBrief.EXISTS

    async def site_clock(self) -> datetime | None:
        """探测站点服务器时间（FR-3.0.2）。m-team 无公开时间 API，
        从 ``Date`` 响应头抓（秒级精度足够）。"""
        try:
            resp = await self._client.head(
                f"{self._base_url()}/", timeout=self._timeout
            )
        except httpx.HTTPError:
            return None
        raw = resp.headers.get("date")
        if not raw:
            return None
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed

    def capabilities(self) -> frozenset[SiteFeature]:
        return frozenset(
            {
                SiteFeature.RSS,
                SiteFeature.DETAIL_API,
                SiteFeature.CLOCK_API,
                SiteFeature.TIMESTAMP_FIELD,
            }
        )

    async def close(self) -> None:
        await self._client.aclose()


class _BreakerTripped(RuntimeError):
    """m-team 站点熔断（FR-2.2：429/403 立即熔断）。"""


def _stringify_safe(raw: dict[str, Any]) -> str:
    """把 dict 压成可读字符串，便于 FreeWindow.raw_text 调试。"""
    parts: list[str] = []
    for k in ("title", "name", "small_descr", "discount", "free", "free_deadline"):
        if k in raw and raw[k] is not None:
            parts.append(f"{k}={raw[k]}")
    return " ".join(parts)[:2000]


__all__ = ["MteamAdapter", "_BreakerTripped"]
