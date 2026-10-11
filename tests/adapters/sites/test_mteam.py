"""MteamAdapter 契约测试。

覆盖 5 个契约方法 + 内部解析逻辑。
所有 HTTP 调用 mock，不依赖网络。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.sites.base import BreakerTripped, SiteProfile, TorrentBrief
from app.adapters.sites.mteam import MteamAdapter, _TorrentDownloadError

# ---- fixtures ----

PROFILE = SiteProfile(name="m-team", domain="m-team.io")
APIKEY = "test-api-key-xxx"
MINIMAL_TORRENT = b"d4:infod6:lengthi100e4:name8:test.txtee"


@pytest.fixture
def adapter() -> MteamAdapter:
    return MteamAdapter(PROFILE, apikey=APIKEY)


def _search_response(items: list[dict]) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value={"code": 0, "data": {"data": items}})
    return r


# ---- 1. check_reachability ----


async def test_check_reachability_ok(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"code": "0", "data": {"username": "x"}})
    adapter._client.post = AsyncMock(return_value=resp)

    health = await adapter.check_reachability()
    assert health.reachable is True
    assert health.detail == "ok"


async def test_check_reachability_no_apikey() -> None:
    a = MteamAdapter(PROFILE)
    health = await a.check_reachability()
    assert health.reachable is False
    assert "apikey" in health.detail


async def test_check_reachability_auth_fail(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 401
    adapter._client.post = AsyncMock(return_value=resp)
    health = await adapter.check_reachability()
    assert health.reachable is False
    assert "鉴权" in health.detail


# ---- 2. search_torrents ----


async def test_search_returns_items(adapter: MteamAdapter) -> None:
    items = [
        {"id": "100001", "name": "Movie A", "size": 100, "status": {"discount": "FREE"}},
        {"id": "100002", "name": "Movie B", "size": 200, "status": {}},
    ]
    adapter._client.post = AsyncMock(return_value=_search_response(items))

    results = await adapter.search_torrents({"keyword": "Movie"})
    assert len(results) == 2
    assert results[0].title == "Movie A"
    assert results[0].item_id == "100001"
    assert results[0].free_payload == {"status": {"discount": "FREE"}}


async def test_search_no_apikey_returns_empty() -> None:
    a = MteamAdapter(PROFILE)
    assert await a.search_torrents({}) == []


async def test_search_429_trips_breaker(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 429
    adapter._client.post = AsyncMock(return_value=resp)
    with pytest.raises(BreakerTripped):
        await adapter.search_torrents({})


# ---- 3. download_torrent ----


async def test_download_torrent_returns_bytes(adapter: MteamAdapter) -> None:
    adapter._gen_dl_token = AsyncMock(return_value="https://dl.m-team.io/x")  # type: ignore
    dl_resp = MagicMock()
    dl_resp.status_code = 200
    dl_resp.content = MINIMAL_TORRENT
    adapter._client.get = AsyncMock(return_value=dl_resp)

    content = await adapter.download_torrent("100001")
    assert content == MINIMAL_TORRENT


async def test_download_torrent_gen_token_fail(adapter: MteamAdapter) -> None:
    adapter._gen_dl_token = AsyncMock(return_value=None)  # type: ignore
    with pytest.raises(_TorrentDownloadError):
        await adapter.download_torrent("100001")


async def test_download_torrent_non_bencode(adapter: MteamAdapter) -> None:
    adapter._gen_dl_token = AsyncMock(return_value="https://dl/x")  # type: ignore
    dl_resp = MagicMock()
    dl_resp.status_code = 200
    dl_resp.content = b"<html>not torrent</html>"
    adapter._client.get = AsyncMock(return_value=dl_resp)
    with pytest.raises(_TorrentDownloadError):
        await adapter.download_torrent("100001")


# ---- 4. check_status ----


async def test_check_status_exists(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"code": "0", "data": {"id": "1", "status": {}}})
    adapter._client.post = AsyncMock(return_value=resp)
    assert await adapter.check_status("1") == TorrentBrief.EXISTS


async def test_check_status_deleted(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"code": "1", "message": "not found"})
    adapter._client.post = AsyncMock(return_value=resp)
    assert await adapter.check_status("1") == TorrentBrief.DELETED


async def test_check_status_404(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 404
    adapter._client.post = AsyncMock(return_value=resp)
    assert await adapter.check_status("1") == TorrentBrief.DELETED


async def test_check_status_no_apikey() -> None:
    a = MteamAdapter(PROFILE)
    assert await a.check_status("1") == TorrentBrief.UNKNOWN


# ---- 5. get_torrent_detail ----


async def test_get_torrent_detail_ok(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"code": "0", "data": {"id": "1", "name": "X", "size": 100, "status": {"discount": "FREE"}}})
    adapter._client.post = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("1")
    assert detail.item_id == "1"
    assert detail.title == "X"
    assert detail.size == 100
    assert detail.error is None
    assert detail.free_payload == {"status": {"discount": "FREE"}}


async def test_get_torrent_detail_error(adapter: MteamAdapter) -> None:
    resp = MagicMock()
    resp.status_code = 403
    adapter._client.post = AsyncMock(return_value=resp)
    detail = await adapter.get_torrent_detail("1")
    assert detail.error is not None


async def test_get_torrent_detail_no_apikey() -> None:
    a = MteamAdapter(PROFILE)
    detail = await a.get_torrent_detail("1")
    assert detail.error == "缺少 apikey"


# ---- _parse_iso_dt ----


def test_parse_iso_dt_unix_seconds() -> None:

    dt = MteamAdapter._parse_iso_dt(1793000000)
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_dt_iso_string() -> None:
    dt = MteamAdapter._parse_iso_dt("2026-10-11T00:33:25Z")
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_dt_none() -> None:
    assert MteamAdapter._parse_iso_dt(None) is None
    assert MteamAdapter._parse_iso_dt("") is None
