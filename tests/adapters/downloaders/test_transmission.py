"""TransmissionAdapter 测试。

覆盖：会话握手、状态映射、add_torrent 多 URL 拒收、所有契约方法。
风格与 tests/adapters/downloaders/test_capabilities.py 一致（respx mock）。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx
from app.adapters.downloaders.base import (
    AddTorrentPayload,
    DownloaderAdapter,
    DownloaderError,
    TorrentFilter,
    TorrentState,
)
from app.adapters.downloaders.transmission import TransmissionAdapter

RPC_URL = "http://tr.example/transmission/rpc"
SESSION = "test-session-abc"


def _make() -> TransmissionAdapter:
    return TransmissionAdapter(
        name="tr-main",
        url="http://tr.example",
        username="admin",
        password="pw",
    )


def _authed() -> TransmissionAdapter:
    """预填 session-id，跳过 409 握手。"""
    ad = _make()
    ad._session_id = SESSION
    return ad


# ============================================================ 会话握手


@pytest.mark.asyncio
async def test_ping_ok() -> None:
    ad = _make()
    with respx.mock(base_url="http://tr.example") as mock:
        # 首次：无 SID → 409 + 携带 SID
        # 之后：携带 SID → 200
        mock.post("/transmission/rpc").mock(
            side_effect=[
                httpx.Response(
                    409,
                    headers={"X-Transmission-Session-Id": SESSION},
                ),
                httpx.Response(
                    200,
                    json={"result": "success", "arguments": {}},
                ),
            ]
        )
        ok = await ad.ping()
    assert ok is True
    await ad.close()


@pytest.mark.asyncio
async def test_ping_fail_5xx_returns_false() -> None:
    ad = _make()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(500, text="boom")
        ok = await ad.ping()
    assert ok is False
    await ad.close()


@pytest.mark.asyncio
async def test_first_call_extracts_session_id_on_409() -> None:
    """首次调用无 session-id：409 响应头携带 SID，被缓存复用。"""
    ad = _make()
    with respx.mock(base_url="http://tr.example") as mock:
        route = mock.post("/transmission/rpc").mock(
            side_effect=[
                httpx.Response(
                    409,
                    headers={"X-Transmission-Session-Id": SESSION},
                ),
                httpx.Response(
                    200,
                    json={"result": "success", "arguments": {}},
                ),
            ]
        )
        ok = await ad.ping()
    assert ok is True
    # 第二次（成功）调用应携带 SID header
    second_call = route.calls[1].request
    assert second_call.headers.get("X-Transmission-Session-Id") == SESSION
    await ad.close()


@pytest.mark.asyncio
async def test_session_id_refresh_on_409_retry() -> None:
    """缓存的 session-id 过期时（再次 409），取响应头新值重试。"""
    ad = _make()
    # 预填 session-id 模拟"已登录"
    ad._session_id = "stale-sid"
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").mock(
            side_effect=[
                httpx.Response(
                    409,
                    headers={"X-Transmission-Session-Id": "fresh-sid"},
                ),
                httpx.Response(
                    200,
                    json={"result": "success", "arguments": {}},
                ),
            ]
        )
        ok = await ad.ping()
    assert ok is True
    assert ad._session_id == "fresh-sid"
    await ad.close()


# ============================================================ 错误分类


@pytest.mark.asyncio
async def test_401_raises_permanent() -> None:
    """首次 409 + 后续 401：握手拿到 SID 后真请求返回 401。"""
    ad = _make()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").mock(
            side_effect=[
                httpx.Response(
                    409,
                    headers={"X-Transmission-Session-Id": SESSION},
                ),
                httpx.Response(401),
            ]
        )
        with pytest.raises(DownloaderError) as exc:
            await ad.add_torrent(
                AddTorrentPayload(name="x", urls=("magnet:?xt=urn:btih:1" * 4,))
            )
    assert exc.value.transient is False
    await ad.close()


@pytest.mark.asyncio
async def test_5xx_raises_transient() -> None:
    """首次 409 + 后续 502：5xx 进重试队列。"""
    ad = _make()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").mock(
            side_effect=[
                httpx.Response(
                    409,
                    headers={"X-Transmission-Session-Id": SESSION},
                ),
                httpx.Response(502),
            ]
        )
        with pytest.raises(DownloaderError) as exc:
            await ad.add_torrent(
                AddTorrentPayload(name="x", urls=("magnet:?xt=urn:btih:1" * 4,))
            )
    assert exc.value.transient is True
    await ad.close()


# ============================================================ add_torrent


@pytest.mark.asyncio
async def test_add_torrent_rejects_multiple_urls() -> None:
    """决策 1：多 URL 直接拒收，不发起请求。"""
    ad = _make()
    payload = AddTorrentPayload(
        name="multi",
        urls=("magnet:?xt=urn:btih:a" * 8, "magnet:?xt=urn:btih:b" * 8),
    )
    with (
        respx.mock(base_url="http://tr.example") as mock,
        pytest.raises(DownloaderError) as exc,
    ):
        await ad.add_torrent(payload)
    assert exc.value.transient is False
    assert "不支持同时多个" in str(exc.value)
    assert len(mock.calls) == 0
    await ad.close()


@pytest.mark.asyncio
async def test_add_torrent_rejects_bytes_with_urls() -> None:
    ad = _make()
    payload = AddTorrentPayload(
        name="bad",
        urls=("magnet:?xt=urn:btih:a" * 8,),
        torrent_bytes=b"d10:...",
    )
    with (
        respx.mock(base_url="http://tr.example") as mock,
        pytest.raises(DownloaderError) as exc,
    ):
        await ad.add_torrent(payload)
    assert exc.value.transient is False
    assert "互斥" in str(exc.value)
    assert len(mock.calls) == 0
    await ad.close()


@pytest.mark.asyncio
async def test_add_torrent_requires_urls_or_bytes() -> None:
    ad = _make()
    payload = AddTorrentPayload(name="empty")
    with (
        respx.mock(base_url="http://tr.example") as mock,
        pytest.raises(DownloaderError) as exc,
    ):
        await ad.add_torrent(payload)
    assert exc.value.transient is False
    assert len(mock.calls) == 0
    await ad.close()


@pytest.mark.asyncio
async def test_add_torrent_single_magnet() -> None:
    ad = _authed()
    payload = AddTorrentPayload(
        name="m1",
        urls=("magnet:?xt=urn:btih:deadbeef" * 4,),
        save_path="/data/m1",
    )
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={
                "result": "success",
                "arguments": {
                    "torrent-added": {
                        "hashString": "ab" * 20,
                        "id": 42,
                    }
                },
            },
        )
        handle = await ad.add_torrent(payload)
        body = json.loads(mock.calls[0].request.content)
        assert body["method"] == "torrent-add"
        args = body["arguments"]
        assert args["filename"] == payload.urls[0]
        assert args["download-dir"] == "/data/m1"
        assert "metainfo" not in args
    assert handle.info_hash == "ab" * 20
    assert handle.downloader == "tr-main"
    await ad.close()


@pytest.mark.asyncio
async def test_add_torrent_single_bytes_uses_metainfo() -> None:
    ad = _authed()
    raw = b"d10:fake-data"
    payload = AddTorrentPayload(name="b1", torrent_bytes=raw)
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={
                "result": "success",
                "arguments": {
                    "torrent-added": {
                        "hashString": "cd" * 20,
                        "id": 7,
                    }
                },
            },
        )
        handle = await ad.add_torrent(payload)
        body = json.loads(mock.calls[0].request.content)
        args = body["arguments"]
        assert args["metainfo"] == base64.b64encode(raw).decode()
        assert "filename" not in args
    assert handle.info_hash == "cd" * 20
    await ad.close()


@pytest.mark.asyncio
async def test_add_torrent_duplicate_returns_handle() -> None:
    ad = _authed()
    payload = AddTorrentPayload(
        name="dup", urls=("magnet:?xt=urn:btih:feed" * 4,),
    )
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={
                "result": "success",
                "arguments": {
                    "torrent-duplicate": {"hashString": "ef" * 20, "id": 99},
                },
            },
        )
        handle = await ad.add_torrent(payload)
    assert handle.info_hash == "ef" * 20
    await ad.close()


# ============================================================ list_torrents


@pytest.mark.asyncio
async def test_list_torrents_empty() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {"torrents": []}},
        )
        rows = await ad.list_torrents()
    assert rows == []
    await ad.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "rate_dl", "percent", "expected"),
    [
        (0, 0, 0.0, TorrentState.PAUSED_DL),
        (0, 0, 1.0, TorrentState.PAUSED_UP),
        (1, 0, 0.5, TorrentState.CHECKING),
        (2, 0, 0.5, TorrentState.CHECKING),
        (3, 0, 0.5, TorrentState.DOWNLOADING),
        (4, 0, 0.5, TorrentState.DOWNLOADING),
        (5, 0, 0.5, TorrentState.SEEDING),
        (6, 0, 1.0, TorrentState.SEEDING),
        (6, 100, 1.0, TorrentState.UPLOADING),
        (99, 0, 0.5, TorrentState.UNKNOWN),
    ],
)
async def test_status_to_state_table(status, rate_dl, percent, expected) -> None:
    """TR status 码映射覆盖：每个状态码至少一例。"""
    ad = _make()
    row = {
        "hashString": "ab" * 20,
        "name": "n",
        "status": status,
        "errorString": "",
        "totalSize": 0,
        "percentDone": percent,
        "downloadedEver": 0,
        "uploadedEver": 0,
        "downloadDir": "/d",
        "rateDownload": rate_dl,
        "rateUpload": 0,
    }
    info = TransmissionAdapter._row_to_info(row)
    assert info.state == expected
    await ad.close()


@pytest.mark.asyncio
async def test_error_string_overrides_to_error() -> None:
    ad = _make()
    row = {
        "hashString": "ab" * 20,
        "name": "n",
        "status": 4,
        "errorString": "Tracker gave HTTP 404",
        "totalSize": 0,
        "percentDone": 0.0,
        "downloadedEver": 0,
        "uploadedEver": 0,
        "downloadDir": "/d",
        "rateDownload": 0,
        "rateUpload": 0,
    }
    info = TransmissionAdapter._row_to_info(row)
    assert info.state == TorrentState.ERROR
    await ad.close()


@pytest.mark.asyncio
async def test_list_torrents_filters_by_state() -> None:
    ad = _authed()
    rows = [
        {"hashString": "a" * 40, "name": "dl", "status": 4,
         "errorString": "", "totalSize": 1, "percentDone": 0.5,
         "downloadedEver": 0, "uploadedEver": 0,
         "downloadDir": "/d", "rateDownload": 0, "rateUpload": 0},
        {"hashString": "b" * 40, "name": "se", "status": 6,
         "errorString": "", "totalSize": 1, "percentDone": 1.0,
         "downloadedEver": 1, "uploadedEver": 0,
         "downloadDir": "/d", "rateDownload": 0, "rateUpload": 0},
    ]
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {"torrents": rows}},
        )
        filt = TorrentFilter(states=frozenset({TorrentState.SEEDING}))
        infos = await ad.list_torrents(filt)
    assert len(infos) == 1
    assert infos[0].state == TorrentState.SEEDING
    await ad.close()


# ============================================================ remove / set_pause


@pytest.mark.asyncio
async def test_remove_with_delete_data() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {}},
        )
        ok = await ad.remove(["a" * 40], delete_data=True)
        body = json.loads(mock.calls[0].request.content)
        assert body["method"] == "torrent-remove"
        assert body["arguments"]["delete-local-data"] is True
    assert ok is True
    await ad.close()


@pytest.mark.asyncio
async def test_remove_without_delete_data() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {}},
        )
        ok = await ad.remove(["a" * 40], delete_data=False)
        body = json.loads(mock.calls[0].request.content)
        assert body["arguments"]["delete-local-data"] is False
    assert ok is True
    await ad.close()


@pytest.mark.asyncio
async def test_remove_empty_hashes_is_noop() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        ok = await ad.remove([], delete_data=False)
    assert ok is True
    assert len(mock.calls) == 0
    await ad.close()


@pytest.mark.asyncio
async def test_set_pause_uses_torrent_stop() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {}},
        )
        ok = await ad.set_pause(["a" * 40], paused=True)
        body = json.loads(mock.calls[0].request.content)
        assert body["method"] == "torrent-stop"
    assert ok is True
    await ad.close()


@pytest.mark.asyncio
async def test_set_pause_uses_torrent_start() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {}},
        )
        ok = await ad.set_pause(["a" * 40], paused=False)
        body = json.loads(mock.calls[0].request.content)
        assert body["method"] == "torrent-start"
    assert ok is True
    await ad.close()


# ============================================================ get_files


@pytest.mark.asyncio
async def test_get_files_returns_complete_list() -> None:
    ad = _authed()
    files_resp = {
        "result": "success",
        "arguments": {
            "torrents": [
                {
                    "files": [
                        {"name": "a.mp4", "length": 1024},
                        {"name": "b.nfo", "length": 64},
                    ]
                }
            ]
        },
    }
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(200, json=files_resp)
        files = await ad.get_files("a" * 40)
    assert len(files) == 2
    assert files[0].path == "a.mp4"
    assert files[0].size == 1024
    assert files[1].path == "b.nfo"
    await ad.close()


@pytest.mark.asyncio
async def test_get_files_unknown_hash_returns_empty() -> None:
    ad = _authed()
    with respx.mock(base_url="http://tr.example") as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json={"result": "success", "arguments": {"torrents": []}},
        )
        files = await ad.get_files("z" * 40)
    assert files == []
    await ad.close()


# ============================================================ 契约保护


def test_transmission_implements_all_abstracts() -> None:
    """FR-1.3：必须实现全部抽象方法。"""
    import inspect

    abstracts = {
        name
        for name, m in inspect.getmembers(
            DownloaderAdapter, predicate=inspect.isabstract
        )
        if not isinstance(m, property)
    }
    assert abstracts <= {
        "ping",
        "add_torrent",
        "list_torrents",
        "remove",
        "set_pause",
    }


@pytest.mark.asyncio
async def test_close_closes_client() -> None:
    ad = _make()
    await ad.close()
    # 二次 close 报 httpx 已关闭，不应抛自定义错误
    with pytest.raises(Exception):
        await ad._client.post(  # type: ignore[union-attr]
            "http://tr.example/transmission/rpc"
        )


def test_adapter_metadata() -> None:
    ad = TransmissionAdapter("tr", "http://x", "", "")
    assert ad.client_type == "transmission"
    assert ad.name == "tr"