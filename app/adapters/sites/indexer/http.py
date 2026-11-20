"""HTTP 适配（替代 MoviePilot 的 ``app.adapters.network.http``）。

移植的 parser/spider 使用同步 ``RequestUtils`` 与异步 ``AsyncRequestUtils``，
本文件用本项目已有的 ``httpx`` 实现同名接口，行为对齐：

- ``cookies`` 支持 cookie 字符串（``a=1; b=2``）或 dict
- ``proxies`` 支持 MoviePilot 的 ``{"http": "...", "https": "..."}`` 或纯字符串
- 返回 ``httpx.Response``；网络错误返回 ``None``（调用方按失败处理）
- 文本解码按「声明编码 → UTF-8 → GB18030 → Big5」回退
"""

from __future__ import annotations

import contextlib
import re
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

_COOKIE_SPLIT_RE = re.compile(r";\s*")
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset=[\"']?([a-zA-Z0-9_-]+)", re.IGNORECASE
)
_XML_ENCODING_RE = re.compile(rb"encoding=[\"']([a-zA-Z0-9_-]+)", re.IGNORECASE)


def cookie_parse(cookies: Any) -> dict[str, str]:
    """把 cookie 字符串 / dict 统一成 dict。"""
    if not cookies:
        return {}
    if isinstance(cookies, dict):
        return {str(k): str(v) for k, v in cookies.items()}
    result: dict[str, str] = {}
    for part in _COOKIE_SPLIT_RE.split(str(cookies)):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def _proxy_url(proxies: Any) -> str | None:
    if not proxies:
        return None
    if isinstance(proxies, str):
        return proxies
    if isinstance(proxies, dict):
        return proxies.get("https") or proxies.get("http") or None
    return None


def _decode_bytes(raw: bytes, declared: str | None = None) -> str:
    """按优先顺序尝试常见编码解码。"""
    candidates: list[str] = []
    if declared:
        candidates.append(declared)
    head = raw[:4096]
    meta = _META_CHARSET_RE.search(head) or _XML_ENCODING_RE.search(head)
    if meta:
        candidates.append(meta.group(1).decode("ascii", "ignore"))
    candidates.extend(["utf-8", "gb18030", "big5", "latin-1"])
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class RequestUtils:
    """同步 HTTP 客户端（接口对齐 MoviePilot）。"""

    def __init__(
        self,
        headers: dict[str, Any] | None = None,
        cookies: Any = None,
        ua: str | None = None,
        timeout: float = 15,
        referer: str | None = None,
        proxies: Any = None,
        use_session: bool = True,
        **_: Any,
    ) -> None:
        merged_headers = dict(headers or {})
        if ua:
            merged_headers.setdefault("User-Agent", ua)
        if referer:
            merged_headers.setdefault("Referer", referer)
        self._client = httpx.Client(
            headers=merged_headers or None,
            cookies=cookie_parse(cookies) or None,
            timeout=timeout,
            proxy=_proxy_url(proxies),
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ 请求
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        kwargs.pop("allow_redirects", None)
        kwargs.pop("verify", None)
        kwargs.pop("proxies", None)
        cookies = kwargs.pop("cookies", None)
        if cookies:
            self._client.cookies.update(cookie_parse(cookies))
        try:
            response = self._client.request(method.upper(), url, **kwargs)
            response.raise_for_status() if False else None  # 保留 4xx/5xx 给调用方判断
            return response
        except httpx.HTTPError:
            return None

    def get_res(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response | None:
        return self.request("GET", url, params=params, **kwargs)

    def post_res(
        self,
        url: str,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> httpx.Response | None:
        return self.request("POST", url, data=data, json=json, **kwargs)

    def put_res(self, url: str, data: Any = None, **kwargs: Any) -> httpx.Response | None:
        return self.request("PUT", url, data=data, **kwargs)

    def delete_res(self, url: str, **kwargs: Any) -> httpx.Response | None:
        return self.request("DELETE", url, **kwargs)

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> str | None:
        response = self.get_res(url, params=params, **kwargs)
        return None if response is None else self.get_decoded_html_content(response)

    def post(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> str | None:
        response = self.post_res(url, **kwargs)
        return None if response is None else self.get_decoded_html_content(response)

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        response = self.get_res(url, params=params, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def post_json(self, url: str, data: Any = None, json_body: Any = None, **kwargs: Any) -> Any:
        response = self.post_res(url, data=data, json=json_body, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    @contextmanager
    def response_manager(self, method: str, url: str, **kwargs: Any):
        """兼容 MoviePilot 的 ``with response_manager(...) as resp`` 用法。"""
        response = self.request(method, url, **kwargs)
        try:
            yield response
        finally:
            if response is not None:
                response.close()

    def update_headers(self, headers: dict[str, Any]) -> None:
        """合并请求头（移植 spider 会在探测/促销补全时追加头）。"""
        if headers:
            self._client.headers.update(headers)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()

    # ------------------------------------------------------------------ 解码
    @staticmethod
    def get_decoded_html_content(
        response: httpx.Response | None,
        performance_mode: Any = None,
        confidence_threshold: Any = None,
    ) -> str:
        """返回解码后的 HTML 文本；空响应返回空串。"""
        if response is None:
            return ""
        try:
            return _decode_bytes(response.content, response.encoding)
        except Exception:
            return ""

    @staticmethod
    def get_decoded_xml_content(
        response: httpx.Response | None,
        performance_mode: Any = None,
        confidence_threshold: Any = None,
    ) -> str:
        """返回解码后的 XML 文本。"""
        return RequestUtils.get_decoded_html_content(
            response, performance_mode, confidence_threshold
        )

    @staticmethod
    def detect_encoding_from_html_response(
        response: httpx.Response | None,
        performance_mode: Any = None,
        confidence_threshold: Any = None,
    ) -> str | None:
        if response is None:
            return None
        return response.encoding


class AsyncRequestUtils:
    """异步 HTTP 客户端（接口对齐 MoviePilot）。"""

    def __init__(
        self,
        headers: dict[str, Any] | None = None,
        cookies: Any = None,
        ua: str | None = None,
        timeout: float = 15,
        referer: str | None = None,
        proxies: Any = None,
        use_session: bool = True,
        **_: Any,
    ) -> None:
        merged_headers = dict(headers or {})
        if ua:
            merged_headers.setdefault("User-Agent", ua)
        if referer:
            merged_headers.setdefault("Referer", referer)
        self._client = httpx.AsyncClient(
            headers=merged_headers or None,
            cookies=cookie_parse(cookies) or None,
            timeout=timeout,
            proxy=_proxy_url(proxies),
            follow_redirects=True,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        kwargs.pop("allow_redirects", None)
        kwargs.pop("verify", None)
        kwargs.pop("proxies", None)
        cookies = kwargs.pop("cookies", None)
        if cookies:
            self._client.cookies.update(cookie_parse(cookies))
        try:
            return await self._client.request(method.upper(), url, **kwargs)
        except httpx.HTTPError:
            return None

    async def get_res(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response | None:
        return await self.request("GET", url, params=params, **kwargs)

    async def post_res(
        self,
        url: str,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> httpx.Response | None:
        return await self.request("POST", url, data=data, json=json, **kwargs)

    async def put_res(self, url: str, data: Any = None, **kwargs: Any) -> httpx.Response | None:
        return await self.request("PUT", url, data=data, **kwargs)

    async def delete_res(self, url: str, **kwargs: Any) -> httpx.Response | None:
        return await self.request("DELETE", url, **kwargs)

    async def get(
        self, url: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> str | None:
        response = await self.get_res(url, params=params, **kwargs)
        return None if response is None else RequestUtils.get_decoded_html_content(response)

    async def post(self, url: str, **kwargs: Any) -> str | None:
        response = await self.post_res(url, **kwargs)
        return None if response is None else RequestUtils.get_decoded_html_content(response)

    async def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        response = await self.get_res(url, params=params, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def update_headers(self, headers: dict[str, Any]) -> None:
        """合并请求头（与同步版本一致）。"""
        if headers:
            self._client.headers.update(headers)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()

    # 与同步版本共用解码逻辑
    get_decoded_html_content = staticmethod(RequestUtils.get_decoded_html_content)
    get_decoded_xml_content = staticmethod(RequestUtils.get_decoded_xml_content)


def base_url_of(url: str) -> str:
    """``https://host/path`` → ``https://host``。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""
