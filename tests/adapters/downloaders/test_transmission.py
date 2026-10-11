"""TransmissionAdapter 测试。

覆盖：
- TR 状态映射（7 个原生 status → TorrentState）
- 构造 / close 不抛
- 5 个契约方法全部实现（不缺 abstract）
- respx 模拟 RPC 调用验证 add / list / remove / pause / resume / get_files
"""

from __future__ import annotations

import base64

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
from app.adapters.downloaders.transmission import TransmissionAdapter, _TR_STATUS_MAP


# ============================================================ 状态映射


@pytest.mark.parametrize(
    ("status", "percent_done", "expected"),
    [
        (0, 0.0, TorrentState.PAUSED_DL),
        (0, 1.0, TorrentState.PAUSED_UP),
        (1, 0.0, TorrentState.CHECKING),
        (2, 0.5, TorrentState.CHECKING),
        (3, 0.0, TorrentState.STALLED_DL),
        (4, 0.5, TorrentState.DOWNLOADING),
        (5, 1.0, TorrentState.SEEDING),
        (6, 1.0, TorrentState.SEEDING),
        (99, 0.0, TorrentState.UNKNOWN),  # 未知状态
    ],
)
def test_tr_status_mapping(
    status: int, percent_done: float, expected: TorrentState
) -> None:
    row = {"status": status, "percentDone": percent_done}
    assert TransmissionAdapter._map_state(row) is expected


def test_tr_status_map_constants() -> None:
    """TR 原生 1~6 都有映射。"""
    assert _TR_STATUS_MAP[1] is TorrentState.CHECKING
    assert _TR_STATUS_MAP[4] is TorrentState.DOWNLOADING
    assert _TR_STATUS_MAP[6] is TorrentState.SEEDING


# ============================================================ 契约


def test_transmission_implements_all_abstracts() -> None:
    """TransmissionAdapter 必须实现全部抽象方法（FR-1.3）。"""
    TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    import inspect

    abstracts = {
        name
        for name, m in inspect.getmembers(
            DownloaderAdapter, predicate=inspect.isabstract
        )
        if not isinstance(m, property)
    }
    assert abstracts <= {"ping", "add_torrent", "list_torrents", "remove", "set_pause"}


# ============================================================ respx RPC 模拟


def _rpc_response(
    *,
    method: str,
    arguments: dict | None = None,
    result: str = "success",
    tag: int = 1,
) -> dict:
    return {"method": method, "arguments": arguments or {}, "result": result, "tag": tag}


async def test_ping_success() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(
                method="session-stats", arguments={"torrentCount": 0}
            ),
        )
        ok = await adapter.ping()
        assert ok is True


async def test_ping_failure_returns_false() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(500)
        ok = await adapter.ping()
        assert ok is False


async def test_add_torrent_with_magnet() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(
                method="torrent-add",
                arguments={
                    "torrent-added": {
                        "hashString": "abc123",
                        "id": 1,
                        "name": "test",
                    }
                },
            ),
        )
        payload = AddTorrentPayload(
            name="test", urls=["magnet:?xt=urn:btih:abc123"], paused=True
        )
        handle = await adapter.add_torrent(payload)
        assert handle.info_hash == "abc123"
        assert handle.downloader == "tr"


async def test_add_torrent_with_bytes() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(
                method="torrent-add",
                arguments={
                    "torrent-added": {
                        "hashString": "def456",
                        "id": 2,
                        "name": "bytes-test",
                    }
                },
            ),
        )
        payload = AddTorrentPayload(
            name="bytes-test", torrent_bytes=b"d8:announce...", paused=False
        )
        handle = await adapter.add_torrent(payload)
        assert handle.info_hash == "def456"


async def test_add_torrent_missing_payload() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    payload = AddTorrentPayload(name="empty")
    with pytest.raises(DownloaderError, match="缺少"):
        await adapter.add_torrent(payload)


async def test_list_torrents_empty() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(
                method="torrent-get", arguments={"torrents": []}
            ),
        )
        items = await adapter.list_torrents()
        assert items == []


async def test_list_torrents_with_filter() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(
                method="torrent-get",
                arguments={
                    "torrents": [
                        {
                            "id": 1,
                            "hashString": "h1",
                            "name": "seeding-torrent",
                            "status": 6,  # seeding
                            "percentDone": 1.0,
                            "totalSize": 1000,
                            "downloadedEver": 1000,
                            "uploadedEver": 500,
                            "rateDownload": 0,
                            "rateUpload": 100,
                            "uploadRatio": 0.5,
                            "eta": -1,
                            "addedDate": 1700000000,
                            "doneDate": 1700001000,
                            "downloadDir": "/downloads",
                            "labels": ["seed", "qa"],
                            "peersConnected": 5,
                            "isFinished": True,
                        },
                        {
                            "id": 2,
                            "hashString": "h2",
                            "name": "downloading-torrent",
                            "status": 4,  # downloading
                            "percentDone": 0.5,
                            "totalSize": 2000,
                            "downloadedEver": 1000,
                            "uploadedEver": 0,
                            "rateDownload": 500,
                            "rateUpload": 0,
                            "uploadRatio": 0.0,
                            "eta": 60,
                            "addedDate": 1700002000,
                            "doneDate": 0,
                            "downloadDir": "/downloads",
                            "labels": [],
                            "peersConnected": 10,
                            "isFinished": False,
                        },
                    ]
                },
            ),
        )
        filt = TorrentFilter(states=frozenset({TorrentState.SEEDING}))
        items = await adapter.list_torrents(filt)
        assert len(items) == 1
        assert items[0].hash == "h1"
        assert items[0].state is TorrentState.SEEDING
        assert items[0].state.is_completed
        assert items[0].tags == frozenset({"seed", "qa"})


async def test_remove_torrents() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(method="torrent-remove"),
        )
        ok = await adapter.remove(["h1", "h2"], delete_data=False)
        assert ok is True


async def test_remove_empty_hashes_returns_true() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    ok = await adapter.remove([], delete_data=False)
    assert ok is True


async def test_set_pause_stop() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(method="torrent-stop"),
        )
        ok = await adapter.set_pause(["h1"], paused=True)
        assert ok is True


async def test_set_pause_start_now() -> None:
    """resume 走 start_torrents_now（绕过队列约束）。"""
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(method="torrent-start-now"),
        )
        ok = await adapter.set_pause(["h1"], paused=False)
        assert ok is True


async def test_get_files_success() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(
            200,
            json=_rpc_response(
                method="torrent-get",
                arguments={
                    "torrents": [
                        {
                            "id": 1,
                            "files": [
                                {"name": "/downloads/file1.mkv", "length": 1000, "priority": 0},
                                {"name": "/downloads/file2.mkv", "length": 2000, "priority": 1},
                            ],
                        }
                    ]
                },
            ),
        )
        files = await adapter.get_files("some-hash")
        assert len(files) == 2
        assert files[0].path == "/downloads/file1.mkv"
        assert files[0].size == 1000
        assert files[1].priority == 1


async def test_get_files_empty_on_error() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    with respx.mock(base_url="http://localhost:9092", assert_all_mocked=False) as mock:
        mock.post("/transmission/rpc").respond(500)
        files = await adapter.get_files("some-hash")
        assert files == []


async def test_close_no_exceptions() -> None:
    adapter = TransmissionAdapter("tr", "http://localhost:9092", "admin", "pass")
    await adapter.close()
