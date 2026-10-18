"""m-team 站点适配器（FR-1.5）。

m-team 公开 JSON API 走 ``x-api-key`` 鉴权（不是 cookie）。

核心端点（host = ``api.<注册域名>``，如 ``api.m-team.cc``）：

- ``POST /api/member/profile``        测连/取用户信息
- ``POST /api/torrent/search``         搜索/列表
- ``POST /api/torrent/detail``         单条详情
- ``POST /api/torrent/genDlToken``     生成下载 URL（需 HMAC-SHA1 签名）

契约方法（5 个）：
1. check_reachability: 调 /api/member/profile
2. search_torrents: 调 /api/torrent/search
3. download_torrent: 走 genDlToken → GET 拿 bencoded bytes
4. check_status: 调 /api/torrent/detail 解析 status 字段
5. get_torrent_detail: 调 /api/torrent/detail

下载流程（download_torrent）：
  1. ``POST /api/torrent/genDlToken``  body={id: <torrent_id>}
  2. 响应 ``data`` 是带 sign/t 的临时下载 URL
  3. 直接 ``GET`` 该 URL 拿 bencoded torrent
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    SiteAdapter,
    SiteHealth,
    SiteProfile,
    TorrentBrief,
    TorrentDetail,
)
from app.adapters.sites.user_data import SiteUserSnapshot
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 单次拉取上限（防止恶意站返回巨大响应撑爆内存）
_MAX_ITEMS_PER_PULL = 500

#: 默认搜索参数可见性：1=正常，2=仅下载过的，3=未下载的（M-Team 官方值）
_DEFAULT_VISIBILITY = 1

#: m-team API 签名密钥（从前端 JS bundle 逆向，公开值）
_API_SECRET = "HLkPcWmycL57mfJt"


class MteamAdapter(SiteAdapter):
    """m-team 站点 JSON API 适配器（x-api-key 鉴权）。"""

    adapter_type = "mteam"

    def __init__(
        self,
        profile: SiteProfile,
        *,
        cookies: str | None = None,
        apikey: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(profile, cookies=cookies, apikey=apikey, timeout=timeout)
        #: 分类 id → 中文名 缓存（首次 search_torrents 时从 categoryList 拉）
        self._category_map: dict[str, str] | None = None
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
        domain = (self.profile.domain or "").strip()
        if not domain:
            return ""
        # 先剥 scheme，避免 domain 含 https:// 时拼出双 scheme
        domain = domain.removeprefix("https://").removeprefix("http://")
        host = domain if domain.startswith("api.") else f"api.{domain}"
        return f"https://{host}"

    def _detail_url(self, item_id: str) -> str:
        domain = (self.profile.domain or "").strip()
        domain = domain.removeprefix("https://").removeprefix("http://")
        return f"https://{domain}/detail/{item_id}"

    def _auth_headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._apikey:
            h["x-api-key"] = self._apikey
        if self._cookies:
            h["Cookie"] = self._cookies
        return h

    async def _load_category_map(self) -> dict[str, str]:
        """拉取 m-team 分类列表（id → 中文名），缓存到实例。

        搜索接口只返回分类数字 id（如 ``"419"``），此映射把 id 转成可读名称
        （``nameChs``）。失败时返回空 dict，调用方回退到原始 id。
        """
        if self._category_map is not None:
            return self._category_map
        empty: dict[str, str] = {}
        if not (self._apikey and self._api_base()):
            self._category_map = empty
            return empty
        try:
            resp = await self._client.post(
                f"{self._api_base()}/api/torrent/categoryList",
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={},
                timeout=self._timeout,
            )
            payload = resp.json()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            logger.debug("m-team 分类列表拉取失败：%s", exc)
            self._category_map = empty
            return empty
        data = payload.get("data") or {}
        rows = data.get("list") if isinstance(data, dict) else None
        mapping: dict[str, str] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                cid = str(row.get("id") or "")
                name = row.get("nameChs") or row.get("nameEng") or ""
                if cid and name:
                    mapping[cid] = str(name)
        self._category_map = mapping
        return mapping

    @staticmethod
    def _api_sign(path: str, ts_ms: int, method: str = "POST") -> str:
        msg = f"{method}&{path}&{ts_ms}"
        return hmac.new(_API_SECRET.encode(), msg.encode(), hashlib.sha1).hexdigest()

    def _signed_form(self, path: str, **fields: Any) -> str:
        ts = int(time.time())
        data: dict[str, Any] = {"_timestamp": str(ts), "_sign": self._api_sign(path, ts)}
        for k, v in fields.items():
            data[k] = str(v) if v is not None else ""
        return urlencode(data)

    @staticmethod
    def _parse_iso_dt(value: object) -> str | None:
        """Unix 秒 / ISO 8601 / 数字字符串 → 北京时间字符串。

        mteam API 返回的 ``createdDate`` / ``lastModifiedDate`` / ``endDate`` 等
        字符串**无时区标记**，实际是 HKT（=CST，UTC+8）本地时间。本函数：
        - Unix 秒：按 UTC 解析，转北京时间字符串
        - ISO 字符串含 ``Z`` 或 ``+HH:MM``：按自带时区解析，转北京时间字符串
        - ISO 字符串**无时区标记**：视为北京时间，格式化后返回
        """
        from datetime import UTC, datetime

        from app.core.timeutils import format_bj, to_bj

        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            if 1_000_000_000 < ts < 4_102_444_800:
                dt = datetime.fromtimestamp(ts, UTC)
                return format_bj(dt)
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
            if dt.tzinfo is not None:
                return format_bj(dt)
            # 无时区标记：naive 视为北京时间（astimezone 会把 naive 当系统本地
            # 时区，容器 TZ=UTC 时产生 ±8h 偏移，必须用 to_bj 显式按北京解释）
            return to_bj(dt)
        return None

    # ------------------------------------------------------------ 1. check_reachability
    async def check_reachability(self) -> SiteHealth:
        """1. 校验域名可达与登录态：调 ``/api/member/profile``。"""
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

    # ------------------------------------------------------------ 2. search_torrents
    async def search_torrents(
        self,
        query: dict[str, object],
        *,
        page_size: int = 200,
        page_number: int = 1,
    ) -> list[FeedItem]:
        """2. 种子搜索：调 ``/api/torrent/search``。

        ``query`` 键支持：
        - keyword: str 关键字
        - categories: list[str] 类目
        - free_only: bool 仅免费

        返回的条目不带下载 URL；下载时调 :meth:`get_torrent_url`，内部分两
        阶段：(1) ``/api/torrent/genDlToken`` 拿临时下载 URL；(2) 返回
        该 URL。
        """
        if not self._apikey:
            return []
        body: dict[str, Any] = {
            "pageNumber": page_number,
            "pageSize": min(page_size, _MAX_ITEMS_PER_PULL),
            "visible": _DEFAULT_VISIBILITY,
        }
        if query.get("keyword"):
            body["keyword"] = str(query["keyword"])
        if "categories" in query and isinstance(query["categories"], list):
            body["categories"] = list(query["categories"])
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
            raise BreakerTripped(f"m-team {self.name} 返回 {resp.status_code}，需熔断")
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
        category_map = await self._load_category_map()
        items: list[FeedItem] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            status = raw.get("status") or {}
            torrent_id = str(raw.get("id") or "")
            if not torrent_id:
                continue
            raw_cat = str(raw.get("category") or "")
            items.append(
                FeedItem(
                    title=raw.get("name") or "",
                    link=self._detail_url(torrent_id),
                    item_id=torrent_id,
                    # m-team 搜索响应直接带 infoHash（小写 hex）
                    info_hash=(str(raw["infoHash"]).lower() or None)
                    if raw.get("infoHash")
                    else None,
                    size=int(raw.get("size") or 0),
                    # 分类 id → 中文名（categoryList 映射；缺失则回退原始 id）
                    category=category_map.get(raw_cat, raw_cat) or None,
                    description=raw.get("smallDescr") or "",
                    published_at=MteamAdapter._parse_iso_dt(raw.get("createdDate")),
                    free_payload={"status": status} if status else None,
                )
            )
            if len(items) >= _MAX_ITEMS_PER_PULL:
                break
        return items

    # ------------------------------------------------------------ 2.5 get_torrent_url
    async def get_torrent_url(self, item_id: str) -> str | None:
        """返回 m-team 临时下载 URL（``genDlToken`` 产物）。

        流程：POST ``/api/torrent/genDlToken``（form + HMAC-SHA1 签名）→
        拿到带签名的临时 URL。每次调用都现拿，无缓存（token 一次性、TTL 短）。
        """
        if not item_id:
            return None
        return await self._gen_dl_token(item_id)

    # ------------------------------------------------------------ 3. download_torrent
    async def download_torrent(self, item_id: str) -> bytes:
        """3. 下载 torrent 原始字节。

        走 ``/api/torrent/genDlToken``（form POST + HMAC-SHA1 签名）拿临时下载 URL，
        再 GET 该 URL 拿 bencoded torrent bytes。
        """
        url = await self._gen_dl_token(item_id)
        if not url:
            raise _TorrentDownloadError(f"m-team genDlToken 失败 id={item_id}")
        try:
            resp = await self._client.get(
                str(url),
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise _TorrentDownloadError(f"下载 torrent 失败：{exc}") from exc
        if resp.status_code >= 400:
            raise _TorrentDownloadError(f"HTTP {resp.status_code}")
        content = resp.content
        if not content.startswith(b"d"):
            raise _TorrentDownloadError("响应不是 bencoded torrent")
        return content

    async def _gen_dl_token(self, item_id: str) -> str | None:
        if not (self._apikey and self._api_base()):
            return None
        path = "/api/torrent/genDlToken"
        body = self._signed_form(path, id=str(item_id))
        try:
            resp = await self._client.post(
                f"{self._api_base()}{path}",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except (TypeError, ValueError):
            return None
        if str(data.get("code")) != "0":
            return None
        return str(data.get("data") or "") or None

    # ------------------------------------------------------------ 4. check_status
    async def check_status(self, item_id: str) -> TorrentBrief:
        """4. 种子状态检查：调 ``/api/torrent/detail`` 解析 status 块。

        m-team 状态约定（实际响应字段）：
        - 200 + status 存在 → EXISTS
        - 200 + status 标记 deleted → DELETED
        - 200 + status 标记 violation → VIOLATION
        - 401/403 → UNKNOWN（鉴权失败不应误判为删除）
        - 404 → DELETED
        - 其他 → UNKNOWN
        """
        if not self._apikey:
            return TorrentBrief.UNKNOWN
        path = "/api/torrent/detail"
        try:
            resp = await self._client.post(
                f"{self._api_base()}{path}",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=f"id={item_id}",
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            return TorrentBrief.UNKNOWN
        if resp.status_code == 404:
            return TorrentBrief.DELETED
        if resp.status_code in (401, 403):
            return TorrentBrief.UNKNOWN
        if resp.status_code >= 400:
            return TorrentBrief.UNKNOWN
        try:
            data = resp.json()
        except (TypeError, ValueError):
            return TorrentBrief.UNKNOWN
        if str(data.get("code")) != "0":
            # code 非 0（如 "该种子不存在"）→ DELETED
            return TorrentBrief.DELETED
        payload = data.get("data") or {}
        if not isinstance(payload, dict):
            return TorrentBrief.UNKNOWN
        # m-team 实际响应：data.status 块或顶层 status 字段
        status = payload.get("status") or {}
        if isinstance(status, dict):
            # 显式删除/违规标记（不同部署字段名可能不同）
            if status.get("deleted") is True or status.get("removed") is True:
                return TorrentBrief.DELETED
            if status.get("violation") is True:
                return TorrentBrief.VIOLATION
        return TorrentBrief.EXISTS

    # ------------------------------------------------------------ 5. get_torrent_detail
    async def get_torrent_detail(self, item_id: str) -> TorrentDetail:
        """5. 种子详情：调 ``/api/torrent/detail``。

        返回统一 TorrentDetail（含 title/size/download_url/free_payload）。
        """
        if not self._apikey or not self._api_base():
            return TorrentDetail(item_id=item_id, error="缺少 apikey")
        path = "/api/torrent/detail"
        try:
            resp = await self._client.post(
                f"{self._api_base()}{path}",
                headers={
                    **self._auth_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=f"id={item_id}",
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return TorrentDetail(item_id=item_id, error=f"detail 网络错误：{exc}")
        if resp.status_code in (401, 403):
            return TorrentDetail(item_id=item_id, error="鉴权失败")
        if resp.status_code >= 400:
            return TorrentDetail(item_id=item_id, error=f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except (TypeError, ValueError):
            return TorrentDetail(item_id=item_id, error="响应非 JSON")
        if str(data.get("code")) != "0":
            return TorrentDetail(
                item_id=item_id, error=data.get("message", "detail 失败")
            )
        d = data.get("data") or {}
        if not isinstance(d, dict):
            return TorrentDetail(item_id=item_id, error="响应 data 非 dict")
        status = d.get("status") or {}
        return TorrentDetail(
            item_id=str(d.get("id") or item_id),
            title=d.get("name"),
            size=int(d.get("size") or 0),
            free_payload={"status": status} if status else None,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ----------------------- FR-1.6 站点用户数据（精简版） -----------------------

    async def get_user_snapshot(self) -> SiteUserSnapshot | None:
        """m-team 走 ``/api/member/profile`` 拉取 4 业务字段。

        **已知限制**（实测 2026-10-18）：m-team 公开 ``/api/member/profile``
        端点返回 31 个 keys（id/username/email/role/memberStatus 等），
        **不暴露** upload/download/seeding/leeching 流量字段。
        拿 4 业务字段需调 m-team 内部其他端点（cookie 模式 + 私有 API），
        单 apikey 模式不支持。

        行为：调通即返回 SiteUserSnapshot（4 字段 0），让 service 写
        ``last_seen_at`` 标记已访问。UI 可识别为"m-team 公开 API 限制"。
        """
        if not self._apikey or not self._api_base():
            return None
        try:
            resp = await self._client.post(
                f"{self._api_base()}/api/member/profile",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("mteam get_user_snapshot HTTP 失败：%s", exc)
            return None
        if resp.status_code >= 400:
            return None
        try:
            data = resp.json()
        except (TypeError, ValueError):
            return None
        if str(data.get("code")) != "0" or not data.get("data"):
            return None
        d = data["data"]
        if not isinstance(d, dict):
            return None
        # 公开 /api/member/profile 端点不暴露 upload/download/seeding/leeching
        # 字段（实测 31 个 keys 全是 profile 元信息，无流量/做种）。
        # 已知 m-team 限制：4 业务字段无法从单端点拿 → 写 0 + last_seen_at。
        # 兜底返回非 None 写时间戳，让 service 标"已访问"（区别于 cookie 失效）。
        upload = int(
            d.get("uploaded") or d.get("uploadedBytes")
            or d.get("uploadedAmount") or 0
        )
        download = int(
            d.get("downloaded") or d.get("downloadedBytes") or 0
        )
        seeding_count = int(
            d.get("seeding") or d.get("seedingCount") or 0
        )
        leeching_count = int(
            d.get("leeching") or d.get("leechingCount") or 0
        )
        return SiteUserSnapshot(
            upload=upload,
            download=download,
            seeding_count=seeding_count,
            leeching_count=leeching_count,
        )


class _TorrentDownloadError(RuntimeError):
    """下载 torrent 失败（genDlToken 失败 / HTTP 错误 / 非 bencoded）。"""


__all__ = ["MteamAdapter", "_TorrentDownloadError"]
