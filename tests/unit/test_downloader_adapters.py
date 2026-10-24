"""下载器适配器字段契约测试。

固化两个跨站语义差异的口径（monitor 的「已完成」判定依赖它们）：

**``completed_bytes``（已完成字节数）**

- qBittorrent：取 ``row["completed"]``（libtorrent ``total_wanted_done``）
- Transmission：取 ``row["haveValid"]``（TR RPC 的「已校验有效字节数」）

**不能**用 ``downloaded``：qB 的 ``downloaded`` 取 ``completed`` 口径正确，
但 TR 的 ``downloaded`` 是 ``downloadedEver``——**累计**下载量（含重复下载），
两站语义不一致，混用会误判「已完成」。

**为什么必须单独一个字段**：``progress`` 在**校验中**返回的是「校验进度」
而非下载完成度（qBittorrent ``TorrentImpl::progress()`` 源码：

.. code-block:: cpp

    if (isChecking()) return m_nativeStatus.progress;

libtorrent 侧该值 = 已校验分片数 / 总分片数）。故 ``progress >= 1.0``
不能用来判断「已完成」，必须走字节口径。

HTTP 层用 respx mock（与仓库既有约定一致），走**真实适配器**代码路径。
"""

from __future__ import annotations

from typing import Any

import pytest
import respx
from app.adapters.downloaders.base import TorrentState

QB_URL = "http://qb.test:8081"
#: TR 适配器会在传入 url 后追加 ``/transmission/rpc``
TR_BASE_URL = "http://tr.test:9091"
TR_URL = f"{TR_BASE_URL}/transmission/rpc"


# ================================================================ qBittorrent


def _qb_row(**over: Any) -> dict[str, Any]:
    """一条 qB ``/api/v2/torrents/info`` 记录。"""
    row: dict[str, Any] = {
        "hash": "a" * 40,
        "name": "t",
        "state": "downloading",
        "progress": 0.5,
        "size": 1000,
        "total_size": 1000,
        "completed": 500,
        "uploaded": 200,
        "downloaded": 500,
        "save_path": "/downloads",
        "category": "",
        "tags": "",
        "dlspeed": 0,
        "upspeed": 0,
        "num_seeds": 0,
        "num_connections": 0,
        "ratio": 0.4,
        "added_on": 0,
        "completion_on": -1,
        "eta": 0,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_qb_completed_bytes_uses_completed_field() -> None:
    """qB：completed_bytes 取 ``completed``（已完成字节数）。"""
    from app.adapters.downloaders.qbittorrent import QbittorrentAdapter

    with respx.mock(assert_all_mocked=False) as router:
        router.get(f"{QB_URL}/api/v2/torrents/info").mock(
            return_value=httpx_response([_qb_row(completed=500, size=1000)])
        )
        adapter = QbittorrentAdapter("qb", QB_URL, "u", "p")
        infos = await adapter.list_torrents()

    assert len(infos) == 1
    assert infos[0].completed_bytes == 500
    assert infos[0].size == 1000


@pytest.mark.asyncio
async def test_qb_checking_state_maps_to_checking() -> None:
    """qB：checkingDL / checkingUP / checkingResumeData 等归一为 CHECKING。

    monitor 靠这个状态跳过校验中的种子（两种模式都跳）。
    """
    from app.adapters.downloaders.qbittorrent import QbittorrentAdapter

    checking_states = [
        "checkingDL",
        "checkingUP",
        "checkingResumeData",
        "moving",
        "metaDL",
        "allocating",
    ]
    rows = [
        _qb_row(hash=str(i) * 40, state=s)
        for i, s in enumerate(checking_states)
    ]
    with respx.mock(assert_all_mocked=False) as router:
        router.get(f"{QB_URL}/api/v2/torrents/info").mock(
            return_value=httpx_response(rows)
        )
        adapter = QbittorrentAdapter("qb", QB_URL, "u", "p")
        infos = await adapter.list_torrents()

    assert [i.state for i in infos] == [TorrentState.CHECKING] * len(
        checking_states
    )


# ================================================================ Transmission


def _tr_row(**over: Any) -> dict[str, Any]:
    """一条 TR ``torrent-get`` 记录。"""
    row: dict[str, Any] = {
        "id": 1,
        "hashString": "a" * 40,
        "name": "t",
        "status": 4,
        "errorString": "",
        "totalSize": 1000,
        "percentDone": 0.5,
        "downloadedEver": 9999,  # 累计下载量（故意远大于 totalSize）
        "uploadedEver": 200,
        "haveValid": 500,
        "downloadDir": "/downloads",
        "rateDownload": 0,
        "rateUpload": 0,
        "labels": [],
        "uploadRatio": 0.4,
        "eta": 0,
        "addedDate": 0,
        "doneDate": 0,
        "peersConnected": 0,
        "isFinished": False,
        "files": [],
        "fileStats": [],
        "secondsDownloading": 0,
        "secondsSeeding": 0,
    }
    row.update(over)
    return row


def _mock_tr(router: respx.MockRouter, rows: list[dict[str, Any]]) -> None:
    """mock TR 的 session 握手 + torrent-get。

    记录 ``torrent-get`` 请求的 fields，供「必须带 haveValid」断言用。
    """
    router.post(TR_URL).mock(
        side_effect=_tr_side_effect(rows),
    )


def _tr_side_effect(rows: list[dict[str, Any]]) -> Any:
    async def _handler(request: Any) -> Any:
        import json

        body = json.loads(request.content)
        method = body.get("method")
        if method == "session-get":
            # TR 首次握手必返 409（带 session-id 响应头），客户端据此拿 sid
            if request.headers.get("X-Transmission-Session-Id"):
                return httpx_response({"result": "success", "tag": "seedhelper"})
            return httpx_response(
                {},
                status=409,
                headers={"X-Transmission-Session-Id": "sid-1"},
            )
        if method == "torrent-get":
            fields = list((body.get("arguments") or {}).get("fields") or [])
            _TR_REQUESTED_FIELDS.append(fields)
            return httpx_response(
                {
                    "result": "success",
                    "tag": "seedhelper",
                    "arguments": {"torrents": rows},
                }
            )
        raise AssertionError(f"意外调用 {method}")

    return _handler


#: 记录最近一次 torrent-get 请求的字段列表。
_TR_REQUESTED_FIELDS: list[list[str]] = []


def httpx_response(
    payload: Any, *, status: int = 200, headers: dict[str, str] | None = None
) -> Any:
    """构造 httpx.Response。"""
    import httpx

    return httpx.Response(status, json=payload, headers=headers or {})


@pytest.mark.asyncio
async def test_tr_completed_bytes_uses_have_valid() -> None:
    """TR：completed_bytes 取 ``haveValid``，**不是** downloadedEver。

    ``downloadedEver`` 是累计下载量（本例 9999 > totalSize 1000），
    拿它判「已完成」会误判。
    """
    from app.adapters.downloaders.transmission import TransmissionAdapter

    _TR_REQUESTED_FIELDS.clear()
    with respx.mock(assert_all_mocked=False) as router:
        _mock_tr(router, [_tr_row(haveValid=500, totalSize=1000)])
        adapter = TransmissionAdapter("tr", TR_BASE_URL, "u", "p")
        infos = await adapter.list_torrents()

    assert len(infos) == 1
    assert infos[0].completed_bytes == 500
    assert infos[0].size == 1000
    # downloaded 仍是累计下载量（未改动原有语义）
    assert infos[0].downloaded == 9999


@pytest.mark.asyncio
async def test_tr_requests_have_valid_field() -> None:
    """TR：torrent-get 必须带上 ``haveValid`` 字段，否则取不到。"""
    from app.adapters.downloaders.transmission import TransmissionAdapter

    _TR_REQUESTED_FIELDS.clear()
    with respx.mock(assert_all_mocked=False) as router:
        _mock_tr(router, [_tr_row()])
        adapter = TransmissionAdapter("tr", TR_BASE_URL, "u", "p")
        await adapter.list_torrents()

    assert _TR_REQUESTED_FIELDS, "未发出 torrent-get 请求"
    assert "haveValid" in _TR_REQUESTED_FIELDS[0]


@pytest.mark.asyncio
async def test_tr_checking_status_maps_to_checking() -> None:
    """TR：status 1/2（queued verify / verifying）归一为 CHECKING。"""
    from app.adapters.downloaders.transmission import TransmissionAdapter

    with respx.mock(assert_all_mocked=False) as router:
        _mock_tr(
            router,
            [
                _tr_row(hashString="1" * 40, status=1),
                _tr_row(hashString="2" * 40, status=2),
            ],
        )
        adapter = TransmissionAdapter("tr", TR_BASE_URL, "u", "p")
        infos = await adapter.list_torrents()

    assert [i.state for i in infos] == [TorrentState.CHECKING] * 2
