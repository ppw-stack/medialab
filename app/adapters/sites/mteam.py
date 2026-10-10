"""m-team 站点适配器（FR-1.5）。

m-team 公开 JSON API 走 ``x-api-key`` 鉴权（不是 cookie，MoviePilot 同款）。

核心端点（host = ``api.<注册域名>``，如 ``api.m-team.cc``）：

- ``POST /api/member/profile``   测连/取用户信息
- ``POST /api/torrent/search``    搜索/列表（body: keyword/categories/pageNumber/pageSize/visible）
- ``POST /api/torrent/genDlToken``  生成下载 URL（body: {id}，响应 {data: {url}}）

下载流程：
  1. ``POST /api/torrent/genDlToken``  body={id: <torrent_id>}
  2. 响应 ``data.url`` 是带 sign/t 的临时下载 URL
  3. 直接 ``GET`` 该 URL 拿 bencoded torrent

免费字段在 search 响应里（不需要解析详情页/HTML）：

- ``status.discount`` (int): 0=无折扣，100=永久免费
- ``status.discountEndTime`` (Unix 秒): 限时截止
- ``status.mallSingleFree.status == "ONGOING"``: 单种特别免费
- ``promotionRule.discount`` + ``endTime``: 全站促销

RSS 兜底：当 ``apikey`` 缺失时降级到 RSS（无免费标记，识别率 0）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import xml.etree.ElementTree as ET

from app.adapters.sites.base import (
    FeedItem,
    SiteAdapter,
    SiteFeature,
    SiteHealth,
    TorrentBrief,
)
from app.core.logging import get_logger
from app.core.timeutils import ClockOffset
from app.core.windows import FreeWindow
from app.db.models import FreeSource, FreeType

logger = get_logger(__name__)

#: 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: 默认搜索参数可见性：1=正常，2=仅下载过的，3=未下载的（M-Team 官方值）
_DEFAULT_VISIBILITY = 1

#: m-team API 签名密钥（从前端 JS bundle 逆向，公开值）
_API_SECRET = "HLkPcWmycL57mfJt"

#: m-team 影片类目（MoviePilot 总结的 cat id）
_MOVIE_CATEGORIES = ("401", "419", "420", "421", "439", "404", "405")
_TV_CATEGORIES = ("403", "402", "435", "438", "404", "405")


class MteamAdapter(SiteAdapter):
    """m-team 站点 JSON API 适配器（x-api-key 鉴权）。"""

    adapter_type = "mteam"

    def __init__(
        self,
        profile,
        *,
        cookies: str | None = None,
        apikey: str | None = None,
        timeout: float = 20.0,
        clock_offset: ClockOffset | None = None,
    ) -> None:
        super().__init__(profile)
        self._cookies = cookies
        self._apikey = apikey
        self._timeout = timeout
        self._clock_offset = clock_offset or ClockOffset.zero()
        # MoviePilot 同款：浏览器风格 UA + Referer，绕 Cloudflare WAF
        # 注意：只发 Referer，**不要发 Origin**（同花顺会触发 WAF）
        # m-team 实际有 3 个域：
        #   - 前端 kp.m-team.cc / m-team.io（用户登录的页面）
        #   - RSS  rss.m-team.cc / rss.m-team.io
        #   - API  api.m-team.io（仅 .io）
        referer_host = self.profile.domain or "m-team.io"
        if "rss." in referer_host:
            referer_host = referer_host.replace("rss.", "", 1)
        if "api." in referer_host:
            referer_host = referer_host.replace("api.", "", 1)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            cookies=self._parse_cookies(cookies),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                "Referer": f"https://{referer_host}/browse",
            },
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

    def _api_base(self) -> str:
        """API 主机完整 URL。MoviePilot 约定：``https://api.<注册域名>``，如 ``https://api.m-team.cc``。"""
        domain = (self.profile.domain or "").strip()
        if not domain:
            return ""
        # 如果配置已含 api. 前缀，直接用
        host = domain if domain.startswith("api.") else f"api.{domain}"
        # 去掉可能已有的协议前缀再加 https://
        host = host.replace("https://", "").replace("http://", "")
        return f"https://{host}"

    def _detail_url(self, item_id: str) -> str:
        if self.profile.detail_path_template:
            path = self.profile.detail_path_template.format(
                id=item_id, hash=item_id
            )
        else:
            path = f"/detail/{item_id}"
        if not path.startswith("/"):
            path = "/" + path
        return f"https://{self.profile.domain}{path}"

    def _auth_headers(self) -> dict[str, str]:
        """带 ``x-api-key`` 的鉴权头。"""
        h: dict[str, str] = {}
        if self._apikey:
            h["x-api-key"] = self._apikey
        if self._cookies:
            h["Cookie"] = self._cookies
        return h

    @staticmethod
    def _api_sign(path: str, ts_ms: int, method: str = "POST") -> str:
        """m-team API 签名：HMAC-SHA1("{METHOD}&{path}&{ts}")，密钥从前端 JS 逆向。"""
        msg = f"{method}&{path}&{ts_ms}"
        return hmac.new(_API_SECRET.encode(), msg.encode(), hashlib.sha1).hexdigest()

    def _signed_form(self, path: str, **fields: Any) -> str:
        """构造带 ``_timestamp``/``_sign`` 的 form body（MoviePilot 同款）。"""
        ts = int(time.time() * 1000)
        data: dict[str, Any] = {"_timestamp": str(ts), "_sign": self._api_sign(path, ts)}
        for k, v in fields.items():
            data[k] = str(v) if v is not None else ""
        return urlencode(data)

    @staticmethod
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
                return MteamAdapter._parse_iso_dt(int(text))
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
        return None

    # ------------------------------------------------------------ 免费窗口解析
    @staticmethod
    def _classify_from_status(status: dict[str, Any]) -> tuple[str, datetime | None]:
        """从 m-team search API 的 status 块提取免费窗口。

        实际响应（m-team 2026-10）：
        - ``discount`` 是字符串 ``"FREE"`` / ``"NORMAL"`` / 数字（0/100/负数）
        - ``discountEndTime`` 是 ``"2026-10-11 00:33:25"`` 格式（无 T）
        - ``mallSingleFree``: {status: "ONGOING", endDate: ...}
        - ``promotionRule``: {discount: "FREE"/"NORMAL", endTime: ...}
        - ``toppingLevel``/``toppingEndTime``: 置顶（也会免费加速下载）

        优先级：mallSingleFree > discount="FREE" > discountEndTime > discount 数值。
        """
        # 1) 单种特别免费（最优先：m-team 显式标记）
        mall = status.get("mallSingleFree")
        if isinstance(mall, dict) and mall.get("status") == "ONGOING":
            return FreeType.TIMED, MteamAdapter._parse_iso_dt(mall.get("endDate"))

        # 2) 全站促销（promotionRule.discount + endTime）
        promo = status.get("promotionRule")
        if isinstance(promo, dict):
            disc = promo.get("discount", "NORMAL")
            end = MteamAdapter._parse_iso_dt(promo.get("endTime"))
            if disc == "FREE" or disc == 100 or disc == "100":
                return FreeType.PERMANENT, None
            if end is not None:
                return FreeType.TIMED, end

        # 3) 标准 discount 字段（可能是字符串 "FREE"/"PERCENT_50" 或数字）
        disc_raw = status.get("discount")
        if isinstance(disc_raw, str):
            if disc_raw == "FREE":
                return FreeType.PERMANENT, None
            if disc_raw.upper().startswith("PERCENT_"):
                # "PERCENT_50" = 50% 折扣 → 中性种；"PERCENT_25" = 25% 折扣
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
            # 其他字符串（"NORMAL" 等）→ 不是免费
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

        # discountEndTime（"2026-10-11 00:33:25"）
        end = MteamAdapter._parse_iso_dt(status.get("discountEndTime"))
        if end is not None:
            return FreeType.TIMED, end
        return FreeType.UNKNOWN, None

    # ------------------------------------------------------------ 接口
    async def ping(self) -> SiteHealth:
        """测连：调 ``/api/member/profile``。"""
        if not self._apikey:
            return SiteHealth(reachable=False, detail="缺少 API key（apikey）")
        try:
            resp = await self._client.post(
                f"{self._api_base()}/api/member/profile",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return SiteHealth(reachable=False, detail=str(exc))
        if resp.status_code in (401, 403):
            return SiteHealth(reachable=False, detail="鉴权失败（apikey 无效或过期）")
        if resp.status_code >= 500:
            return SiteHealth(reachable=False, detail=f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except (TypeError, ValueError):
            return SiteHealth(reachable=False, detail="响应非 JSON")
        if str(data.get("code")) == "0" and data.get("data"):
            return SiteHealth(reachable=True, detail="ok")
        return SiteHealth(reachable=False, detail=data.get("message", "鉴权已过期或无效"))

    async def fetch_feed(
        self,
        url: str,
        *,
        page_size: int = 100,
        page_number: int = 1,
        categories: list[str] | None = None,
    ) -> list[FeedItem]:
        """拉取种子列表。

        ``url`` 参数兼容 RSS 模式（向后兼容）：当有 apikey 时忽略 url 走 API 模式；
        否则降级到 RSS 解析（识别率 0）。
        """
        if self._apikey and self._api_base():
            return await self._fetch_via_api(
                page_size=page_size,
                page_number=page_number,
                categories=categories,
            )
        return await self._fetch_via_rss(url)

    async def _fetch_via_api(
        self,
        *,
        page_size: int = 100,
        page_number: int = 1,
        categories: list[str] | None = None,
    ) -> list[FeedItem]:
        """调 ``/api/torrent/search`` 拉取 JSON 列表。"""
        body: dict[str, Any] = {
            "pageNumber": page_number,
            "pageSize": min(page_size, _MAX_ITEMS_PER_PULL),
            "visible": _DEFAULT_VISIBILITY,
        }
        if categories:
            body["categories"] = categories
        try:
            resp = await self._client.post(
                f"{self._api_base()}/api/torrent/search",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("m-team %s 搜索失败：%s", self.name, exc)
            return []
        if resp.status_code in (403, 429):
            raise _BreakerTripped(f"m-team {self.name} 返回 {resp.status_code}，需熔断")
        if resp.status_code >= 400:
            logger.warning("m-team %s 搜索返回 %s", self.name, resp.status_code)
            return []
        try:
            payload = resp.json()
        except (TypeError, ValueError) as exc:
            logger.warning("m-team %s 搜索响应非 JSON：%s", self.name, exc)
            return []
        if str(payload.get("code")) != "0":
            logger.warning(
                "m-team %s 搜索 code=%s message=%s",
                self.name, payload.get("code"), payload.get("message"),
            )
            return []
        data = (payload.get("data") or {}).get("data") or []
        items: list[FeedItem] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            status = raw.get("status") or {}
            torrent_id = str(raw.get("id") or "")
            if not torrent_id:
                continue
            items.append(
                FeedItem(
                    title=raw.get("name") or "",
                    link=self._detail_url(torrent_id),
                    info_hash=None,  # m-team search API 不返回 infohash
                    size=int(raw.get("size") or 0),
                    category=str(raw.get("category") or ""),
                    description=raw.get("smallDescr") or "",
                    published_at=MteamAdapter._parse_iso_dt(raw.get("createdDate")),
                    torrent_url=f"api://gen/{torrent_id}",  # 占位，torrent_detail 实际生成
                    # m-team API 免费字段直接塞进 free_payload（不污染 description）
                    free_payload={"status": status} if status else None,
                )
            )
            if len(items) >= _MAX_ITEMS_PER_PULL:
                break
        return items

    async def _fetch_via_rss(self, url: str) -> list[FeedItem]:
        """RSS 兜底（无 apikey 时）。识别率 = 0。"""
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("m-team %s RSS 拉取失败：%s", self.name, exc)
            return []
        if resp.status_code >= 400:
            return []
        return self._parse_rss(resp.text)

    @staticmethod
    def _parse_rss(xml_text: str) -> list[FeedItem]:
        try:
            root = ET.fromstring(xml_text.encode("utf-8"))
        except ET.ParseError as exc:
            logger.warning("m-team RSS XML 解析失败：%s", exc)
            return []
        items: list[FeedItem] = []
        for node in root.iter("item"):
            fields: dict[str, str] = {}
            for c in node:
                local = c.tag.rsplit("}", 1)[-1].lower()
                val = (c.get("href") or c.get("url") or c.text or "").strip()
                if local not in fields:
                    fields[local] = val
            link = fields.get("link") or fields.get("guid") or ""
            if not link:
                continue
            desc = fields.get("description", "")[:20000]
            cat_node = node.find("category")
            enc_node = node.find("enclosure")
            items.append(
                FeedItem(
                    title=fields.get("title", ""),
                    link=link,
                    info_hash=None,
                    size=int(enc_node.get("length") or 0) if enc_node is not None and enc_node.get("length") else 0,
                    category=cat_node.text if cat_node is not None and cat_node.text else None,
                    description=desc,
                    published_at=None,
                    torrent_url=enc_node.get("url") if enc_node is not None and enc_node.get("url") else link,
                )
            )
        return items[:_MAX_ITEMS_PER_PULL]

    # ------------------------------------------------------------ 解析
    async def parse_window(
        self,
        raw: str | dict[str, object],
        *,
        source_hint: str | None = None,
    ) -> FreeWindow:
        """FR-1.5.3：解析单条目的免费窗口。

        dict 路径（API 搜索结果）：从 status 块提取 discount / discountEndTime /
        promotionRule / mallSingleFree。
        str 路径（HTML 详情页或 RSS 描述）：保留"剩余 X"文案兜底。
        """
        if isinstance(raw, dict):
            status = raw.get("status") if isinstance(raw.get("status"), dict) else raw
            free_type, end_at = MteamAdapter._classify_from_status(status)
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
            return FreeWindow(
                free_type=free_type,
                free_end_at=None,
                free_source=FreeSource.SITE_DEFAULT,
                raw_text=_stringify_safe(raw),
                confidence=0.3,
            )
        # str 路径（兼容 HTML 详情页或 RSS description）
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
        import re
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

    # ------------------------------------------------------------ 详情与下载
    async def torrent_detail(self, item_id: str) -> dict[str, object]:
        """拉取详情：调 ``/api/torrent/genDlToken`` 拿下载 URL，返回 ``{id, download_url}``。

        注意：genDlToken 必须用 form POST（application/x-www-form-urlencoded）
        + ``_timestamp``/``_sign`` 签名（MoviePilot 同款），JSON body 会返回"參數錯誤"。
        """
        if self._apikey and self._api_base():
            path = "/api/torrent/genDlToken"
            body = self._signed_form(path, id=str(item_id))
            try:
                resp = await self._client.post(
                    f"{self._api_base()}{path}",
                    headers={**self._auth_headers(), "Content-Type": "application/x-www-form-urlencoded"},
                    content=body,
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                return {"id": item_id, "_error": f"genDlToken 网络错误：{exc}"}
            if resp.status_code in (401, 403):
                return {"id": item_id, "_error": "鉴权失败"}
            if resp.status_code >= 400:
                return {"id": item_id, "_error": f"HTTP {resp.status_code}"}
            try:
                data = resp.json()
            except (TypeError, ValueError):
                return {"id": item_id, "_error": "响应非 JSON"}
            if str(data.get("code")) != "0":
                return {"id": item_id, "_error": data.get("message", "genDlToken 失败")}
            return {"id": item_id, "download_url": str(data.get("data") or "")}
        # 兜底：HTML 详情页
        url = self._detail_url(item_id)
        if not url:
            return {}
        try:
            resp = await self._client.get(url, timeout=self._timeout)
        except httpx.HTTPError:
            return {}
        if resp.status_code >= 400:
            return {}
        return {"_html": resp.text[:20000]}

    async def download_torrent(self, item_id: str) -> tuple[bytes | None, str | None]:
        """下载 torrent 文件，返回 ``(torrent_bytes, infohash)``。

        infohash = torrent 文件 info dict 的 bencoded 表示的 SHA1。
        获取 infohash 的唯一可靠途径（m-team search API 的 ``infoHash`` 恒为 None）。
        """
        detail = await self.torrent_detail(item_id)
        download_url = detail.get("download_url")
        if not download_url:
            return None, detail.get("_error")
        try:
            resp = await self._client.get(
                str(download_url),
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return None, f"下载 torrent 失败：{exc}"
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}"
        content = resp.content
        if not content.startswith(b"d"):
            return None, "响应不是 bencoded torrent"
        info_hash = self._infohash_from_torrent(content)
        return content, info_hash

    async def fetch_infohash(self, item_id: str) -> str | None:
        """m-team search API 不返回 hash，通过下载 torrent 文件解析。

        错误情况返回 None（错误信息丢弃，不影响业务）。
        """
        _content, info_hash = await self.download_torrent(item_id)
        if not info_hash or not isinstance(info_hash, str) or len(info_hash) != 40:
            return None
        return info_hash

    @staticmethod
    def _infohash_from_torrent(torrent_bytes: bytes) -> str | None:
        """从 torrent 文件解析 infohash（info dict 的 bencoded SHA1）。"""
        try:
            info = MteamAdapter._bdecode_info(torrent_bytes)
        except (ValueError, IndexError):
            return None
        encoded = MteamAdapter._bencode(info)
        return hashlib.sha1(encoded).hexdigest()

    @staticmethod
    def _bdecode_info(data: bytes) -> dict[bytes, Any]:
        """解析 torrent 的 info dict。"""
        def _decode(idx: int) -> tuple[Any, int]:
            c = data[idx:idx + 1]
            if c == b'd':
                idx += 1
                d: dict[bytes, Any] = {}
                while data[idx:idx + 1] != b'e':
                    k, idx = _decode(idx)
                    v, idx = _decode(idx)
                    d[k] = v
                return d, idx + 1
            if c == b'l':
                idx += 1
                lst: list[Any] = []
                while data[idx:idx + 1] != b'e':
                    v, idx = _decode(idx)
                    lst.append(v)
                return lst, idx + 1
            if c == b'i':
                idx += 1
                end = data.index(b'e', idx)
                return int(data[idx:end]), end + 1
            if c in b'0123456789':
                end = data.index(b':', idx)
                n = int(data[idx:end])
                return data[end + 1:end + 1 + n], end + 1 + n
            raise ValueError(f"bad bencode char {c!r} at {idx}")

        root, _ = _decode(0)
        if not isinstance(root, dict) or b'info' not in root:
            raise ValueError("missing info dict")
        return root[b'info']

    @staticmethod
    def _bencode(obj: Any) -> bytes:
        if isinstance(obj, int):
            return b'i' + str(obj).encode() + b'e'
        if isinstance(obj, bytes):
            return str(len(obj)).encode() + b':' + obj
        if isinstance(obj, str):
            return MteamAdapter._bencode(obj.encode())
        if isinstance(obj, dict):
            items = sorted(obj.items(), key=lambda x: x[0])
            return b'd' + b''.join(MteamAdapter._bencode(k) + MteamAdapter._bencode(v) for k, v in items) + b'e'
        if isinstance(obj, list):
            return b'l' + b''.join(MteamAdapter._bencode(v) for v in obj) + b'e'
        raise ValueError(f"bad bencode type {type(obj)}")

    async def verify_status(self, info_hash: str) -> TorrentBrief:
        """m-team search API 不暴露删除/违规状态 → UNKNOWN。"""
        return TorrentBrief.UNKNOWN

    async def site_clock(self) -> datetime | None:
        """探测站点服务器时间（FR-3.0.2）。m-team 公开响应头可用。"""
        try:
            resp = await self._client.head(
                f"https://{self.profile.domain}/", timeout=self._timeout
            )
        except httpx.HTTPError:
            return None
        from email.utils import parsedate_to_datetime
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
    for k in ("id", "name", "smallDescr", "discount", "discountEndTime",
              "mallSingleFree", "promotionRule"):
        if k in raw and raw[k] is not None:
            parts.append(f"{k}={raw[k]}")
    return " ".join(parts)[:2000]


__all__ = ["MteamAdapter", "_BreakerTripped"]
