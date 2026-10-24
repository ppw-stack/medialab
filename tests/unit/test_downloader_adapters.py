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


def _tr_free_space_side_effect(payload: dict[str, Any]) -> Any:
    """构造只响应 ``free-space`` 的 TR RPC 处理器。"""

    async def _handler(request: Any) -> Any:
        import json

        body = json.loads(request.content)
        method = body.get("method")
        if method == "session-get":
            if request.headers.get("X-Transmission-Session-Id"):
                return httpx_response({"result": "success", "tag": "seedhelper"})
            return httpx_response(
                {},
                status=409,
                headers={"X-Transmission-Session-Id": "sid-1"},
            )
        if method == "free-space":
            return httpx_response(
                {
                    "result": "success",
                    "tag": "seedhelper",
                    "arguments": payload,
                }
            )
        raise AssertionError(f"意外调用 {method}")

    return _handler


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


# ================================================================ 空间统计字段


@pytest.mark.asyncio
async def test_qb_list_with_errors_returns_space_fields() -> None:
    """qB：``list_torrents_with_errors`` 必须带 ``completed_bytes`` / ``save_path``。

    辅种空间统计靠这两个字段算实际占用与目录分布——缺了就只能算个空。
    """
    from app.adapters.downloaders.qbittorrent import QbittorrentAdapter

    with respx.mock(assert_all_mocked=False) as router:
        router.get(f"{QB_URL}/api/v2/torrents/info").mock(
            return_value=httpx_response(
                [_qb_row(completed=500, size=1000, save_path="/downloads/aux")]
            )
        )
        adapter = QbittorrentAdapter("qb", QB_URL, "u", "p")
        rows = await adapter.list_torrents_with_errors()

    assert len(rows) == 1
    assert rows[0]["completed_bytes"] == 500
    assert rows[0]["save_path"] == "/downloads/aux"


@pytest.mark.asyncio
async def test_tr_list_with_errors_returns_space_fields() -> None:
    """TR：``list_torrents_with_errors`` 必须带 ``completed_bytes`` / ``save_path``。

    TR 的 ``haveValid`` 是**精确**的磁盘占用口径。
    """
    from app.adapters.downloaders.transmission import TransmissionAdapter

    _TR_REQUESTED_FIELDS.clear()
    with respx.mock(assert_all_mocked=False) as router:
        router.post(TR_URL).mock(
            side_effect=_tr_side_effect(
                [_tr_row(haveValid=500, totalSize=1000, downloadDir="/dl/aux")]
            )
        )
        adapter = TransmissionAdapter("tr", TR_BASE_URL, "u", "p")
        rows = await adapter.list_torrents_with_errors()

    assert len(rows) == 1
    assert rows[0]["completed_bytes"] == 500
    assert rows[0]["save_path"] == "/dl/aux"
    # 请求的字段里必须带上这两个
    assert "haveValid" in _TR_REQUESTED_FIELDS[0]
    assert "downloadDir" in _TR_REQUESTED_FIELDS[0]


@pytest.mark.asyncio
async def test_qb_get_disk_space_has_no_total() -> None:
    """qB：只给剩余空间，**总量为 None**（不是 0——0 会被画成「磁盘已满」）。"""
    from app.adapters.downloaders.qbittorrent import QbittorrentAdapter

    with respx.mock(assert_all_mocked=False) as router:
        router.get(f"{QB_URL}/api/v2/sync/maindata").mock(
            return_value=httpx_response(
                {"rid": 0, "server_state": {"free_space_on_disk": 123456}}
            )
        )
        adapter = QbittorrentAdapter("qb", QB_URL, "u", "p")
        space = await adapter.get_disk_space()

    assert space.free_bytes == 123456
    assert space.total_bytes is None
    assert space.used_bytes is None


@pytest.mark.asyncio
async def test_tr_get_disk_space_returns_free_and_total() -> None:
    """TR：``free-space`` 返回剩余 + 总量。"""
    from app.adapters.downloaders.transmission import TransmissionAdapter

    with respx.mock(assert_all_mocked=False) as router:
        router.post(TR_URL).mock(
            side_effect=_tr_free_space_side_effect(
                {"path": "/dl", "size-bytes": 400, "total_size": 1000}
            )
        )
        adapter = TransmissionAdapter("tr", TR_BASE_URL, "u", "p")
        space = await adapter.get_disk_space("/dl")

    assert space.free_bytes == 400
    assert space.total_bytes == 1000
    assert space.used_bytes == 600


# ================================================================ 批量超时


def test_bulk_timeout_is_long_enough_for_cross_seed() -> None:
    """批量路径的超时必须**远大于**默认的 15s。

    辅种站实测：全量 4600+ 个种子的 ``torrent-get`` 要 20s+（TR 繁忙时
    超过 120s）。用默认 15s 会**必然超时**——辅种快照与空间统计都拿不到
    数据（表现为「辅种下载器拉取失败：TR 网络错误」）。
    """
    from app.adapters.downloaders.qbittorrent import (
        BULK_TIMEOUT as QB_BULK_TIMEOUT,
    )
    from app.adapters.downloaders.transmission import (
        BULK_TIMEOUT as TR_BULK_TIMEOUT,
    )

    assert TR_BULK_TIMEOUT >= 60
    assert QB_BULK_TIMEOUT >= 60


@pytest.mark.asyncio
async def test_tr_bulk_list_uses_long_timeout() -> None:
    """TR 的 ``torrent-get`` 必须用长超时（不是默认的 15s）。"""
    from app.adapters.downloaders import transmission as tr_mod

    seen: list[float | None] = []
    original = tr_mod.TransmissionAdapter._rpc

    async def _spy(self: Any, *args: Any, **kw: Any) -> Any:
        seen.append(kw.get("http_timeout"))
        return await original(self, *args, **kw)

    tr_mod.TransmissionAdapter._rpc = _spy  # type: ignore[method-assign]
    try:
        with respx.mock(assert_all_mocked=False) as router:
            _mock_tr(router, [_tr_row()])
            adapter = tr_mod.TransmissionAdapter("tr", TR_BASE_URL, "u", "p")
            await adapter.list_torrents_with_errors()
    finally:
        tr_mod.TransmissionAdapter._rpc = original  # type: ignore[method-assign]

    assert seen, "未调用 _rpc"
    assert seen[0] == tr_mod.BULK_TIMEOUT
    assert seen[0] and seen[0] > 15.0
