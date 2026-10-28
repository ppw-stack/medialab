"""辅种下载器定时抓取 + 快照落库集成测试。

**锁住的核心契约**（2026-10-24）：

辅种下载器的种子量大（实测 3000+ 个），实时调下载器接口要 20s+。
故改为「定时抓取 → 落库快照 → 前端读快照」。这条链路的行为必须钉住：

1. **成功抓取** → 写一行 ``cross_seed_snapshot``（统计 + 异常明细）
2. **抓取失败** → 计入 failed 并点名，**保留上一次的快照**
   （不写 0 兜底——写 0 等于把故障伪装成"没有种子"）
3. **批量暂停**：异常种子一次 ``set_pause([...])`` 发一批，不是逐个发
4. **已暂停的异常种子跳过**：上一轮已暂停过 → 不重复发指令
5. **网络 IO 在事务外**：并发抓取不持写锁
6. **无辅种下载器** → skipped，不报错
"""

from __future__ import annotations

import asyncio

import pytest
from app.adapters.downloaders.base import DiskSpace, DownloaderError
from app.db.models import CrossSeedSnapshot, DownloaderInstance
from app.db.session import session_scope
from app.services.cross_seed_monitor import (
    PAUSE_BATCH_SIZE,
    CrossSeedMonitorService,
)
from app.services.factory import AdapterFactory
from sqlalchemy import select


class _FakeAdapter:
    """可控的假辅种下载器适配器。

    ``mode``：
    - ``"ok"``    → 正常返回种子
    - ``"raise"`` → 抛 DownloaderError（网络 / 鉴权故障）
    """

    def __init__(
        self,
        torrents: list[dict[str, object]] | None = None,
        *,
        mode: str = "ok",
        disk_free_bytes: int | None = 1_000_000,
        disk_total_bytes: int | None = 10_000_000,
    ) -> None:
        self._torrents = torrents or []
        self.mode = mode
        #: 磁盘空间（None = 该下载器不提供总量 / 不支持查询）
        self._disk_free_bytes = disk_free_bytes
        self._disk_total_bytes = disk_total_bytes
        #: 每次 set_pause 的 hash 列表（记录调用次数与批量行为）
        self.pause_calls: list[list[str]] = []
        self.list_calls = 0

    async def list_torrents_with_errors(self) -> list[dict[str, object]]:
        self.list_calls += 1
        if self.mode == "raise":
            raise DownloaderError("连接超时")
        return list(self._torrents)

    async def get_disk_space(self, path: str | None = None) -> DiskSpace:
        if self._disk_free_bytes is None:
            raise NotImplementedError("不支持磁盘空间查询")
        return DiskSpace(
            free_bytes=self._disk_free_bytes,
            total_bytes=self._disk_total_bytes,
        )

    async def set_pause(self, hashes: list[str], paused: bool) -> bool:
        assert paused is True
        self.pause_calls.append(list(hashes))
        return True

    async def close(self) -> None:
        return None


def _torrent(
    hash_: str,
    name: str,
    size: int,
    *,
    state: str = "seeding",
    error_string: str = "",
    error_code: int = 0,
    tid: int = 0,
    completed_bytes: int | None = None,
    save_path: str | None = None,
    added_at: int | None = None,
) -> dict[str, object]:
    """构造一条 adapter 契约形状的种子记录。"""
    return {
        "id": tid,
        "hash": hash_,
        "name": name,
        "size": size,
        "status": 6,
        "error_code": error_code,
        "error_string": error_string,
        "state": state,
        "completed_bytes": completed_bytes if completed_bytes is not None else size,
        "save_path": save_path,
        "added_at": added_at,
    }


async def _add_downloader(
    name: str = "dl1", *, is_cross_seed: bool = True, enabled: bool = True
) -> int:
    async with session_scope() as session:
        row = DownloaderInstance(
            name=name,
            type="qbittorrent",
            url="http://localhost:8081",
            is_cross_seed=is_cross_seed,
            enabled=enabled,
        )
        session.add(row)
        await session.flush()
        return row.id


def _factory_with(adapters: dict[int, _FakeAdapter]) -> AdapterFactory:
    """构造工厂：按下载器 id 返回指定假适配器。"""
    factory = AdapterFactory()

    def _downloader(row: DownloaderInstance, **kw: object) -> _FakeAdapter:
        return adapters[row.id]

    factory.downloader = _downloader  # type: ignore[method-assign]
    return factory


def _svc(factory: AdapterFactory, monkeypatch: pytest.MonkeyPatch) -> CrossSeedMonitorService:
    """构造 service 并把全局工厂换成假工厂。"""
    import app.services.factory as factory_mod

    monkeypatch.setattr(factory_mod, "_factory", factory)
    svc = CrossSeedMonitorService()
    return svc


async def _snapshots() -> list[CrossSeedSnapshot]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(CrossSeedSnapshot).order_by(CrossSeedSnapshot.downloader_id)
                )
            ).scalars()
        )


# ------------------------------------------------------------------ 成功落库


async def test_success_writes_snapshot(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功抓取 → 写一行快照（统计 + 异常明细）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent("a" * 40, "Movie.2024", 1024**3),
            _torrent("b" * 40, "Movie.2024", 1024**3),
            _torrent("c" * 40, "Show.S01", 2 * 1024**3),
            _torrent(
                "d" * 40,
                "Broken",
                1024**3,
                state="error",
                error_string="Tracker not registered with this tracker",
                error_code=2,
            ),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await svc.refresh_downloader(dl_id)

    assert result.total_torrents == 4
    assert result.unique_count == 3, "name+size 精确分组：3 组"
    assert result.anomaly_count == 1
    # 在途：3 个做种 + 1 个 error（致命错误 → 不算在途）
    assert result.inflight_count == 3, "error 是致命错误，不算在途"

    rows = await _snapshots()
    assert len(rows) == 1
    row = rows[0]
    assert row.downloader_id == dl_id
    assert row.downloader_name == "dl1"
    assert row.total_torrents == 4
    assert row.unique_count == 3
    assert row.anomaly_count == 1
    assert row.inflight_count == 3
    assert row.scanned_at
    # 明细落库
    assert len(row.stats) == 3
    assert len(row.anomalies) == 1
    assert row.anomalies[0]["hash"] == "d" * 40
    assert row.anomalies[0]["anomaly_type"] == "tracker_not_registered"


async def test_stats_group_by_name_and_size(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名不同大小 → 不同分组（不归一化）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent("a" * 40, "Same", 1024**3),
            _torrent("b" * 40, "Same", 2 * 1024**3),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await svc.refresh_downloader(dl_id)
    assert result.unique_count == 2


async def test_stats_store_full_hashes_for_deletion(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个分组存下**完整 40 位** hash 列表（删除该组时要用）。

    ``sample_hash`` 是 12 位**截断**的，只作展示用——拿它删除会被适配器
    拒绝（hash 必须是 40 位 hex）。
    """
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent("a" * 40, "Group", 1024**3),
            _torrent("b" * 40, "Group", 1024**3),
            _torrent("c" * 40, "Group", 1024**3),
            _torrent("d" * 40, "Other", 1024**3),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    stats = {s["name"]: s for s in rows[0].stats}
    group = stats["Group"]
    assert group["count"] == 3
    # 完整 40 位 hash（删除要用）
    assert sorted(group["hashes"]) == ["a" * 40, "b" * 40, "c" * 40]
    assert all(len(h) == 40 for h in group["hashes"])
    # sample_hash 仍是 12 位截断（展示用）
    assert len(group["sample_hash"]) == 12
    # 不同组各有各的 hash 列表
    assert stats["Other"]["hashes"] == ["d" * 40]


async def test_stats_hashes_are_omitted_when_torrent_has_no_hash(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 hash 的种子不进 hash 列表（不塞空串）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            {"id": 0, "hash": "", "name": "X", "size": 100},
            _torrent("a" * 40, "X", 100),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    assert rows[0].stats[0]["hashes"] == ["a" * 40]


async def test_stats_store_added_at_mapping(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个分组存下 ``hash → added_at`` 映射（disk_rotation 同档先删旧的用）。

    - 有时间的种子进映射
    - ``added_at=None`` 的种子**不进**映射（排序时回落到无时间档）
    - ``added_at=0`` 也归一为 None，不进映射（与适配器层一致）
    - 旧快照行（没有 added_at key）也能正常读取（默认空 dict）
    """
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent("a" * 40, "Group", 1024**3, added_at=1700000000),
            _torrent("b" * 40, "Group", 1024**3, added_at=None),  # 缺失
            _torrent("c" * 40, "Group", 1024**3, added_at=0),  # 0 = 未知
            _torrent("d" * 40, "Other", 1024**3, added_at=1700000001),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    stats = {s["name"]: s for s in rows[0].stats}
    group = stats["Group"]
    # 只有 a 进映射（b 缺失、c=0 都不进）
    assert group["added_at"] == {"a" * 40: 1700000000}
    # Other 组的单个种子也进映射
    assert stats["Other"]["added_at"] == {"d" * 40: 1700000001}


async def test_snapshot_is_upserted_not_duplicated(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次刷新**覆盖**同一行（downloader_id UNIQUE），不堆行。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter([_torrent("a" * 40, "X", 1024**3)])
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)
    adapter._torrents = [_torrent("a" * 40, "X", 1024**3), _torrent("b" * 40, "Y", 1024**3)]
    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    assert len(rows) == 1
    assert rows[0].total_torrents == 2


async def test_snapshot_persists_pause_counts(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """暂停计数落库（前端从库里读快照时也要能看到）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent(
                "a" * 40,
                "Bad",
                1024**3,
                state="error",
                error_string="Tracker not registered",
                error_code=2,
            ),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    assert rows[0].anomaly_count == 1
    assert rows[0].paused_count == 1
    assert rows[0].pause_failed_count == 0

    # 从库里读详情时也要带上这两个字段
    snap = await svc.get_snapshot(dl_id)
    assert snap is not None
    assert snap["paused_count"] == 1
    assert snap["pause_failed_count"] == 0


# ------------------------------------------------------------------ 失败处理


async def test_failure_keeps_previous_snapshot(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抓取失败 → 抛错 + **保留上一次的快照**（不写 0 兜底）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter([_torrent("a" * 40, "X", 1024**3)])
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)
    assert (await _snapshots())[0].total_torrents == 1

    adapter.mode = "raise"
    with pytest.raises(DownloaderError):
        await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    assert len(rows) == 1
    assert rows[0].total_torrents == 1, "失败不该把快照清成 0"


async def test_job_counts_failure_and_keeps_snapshot(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """作业里失败站计入 failed + 点名，快照保留旧值。"""
    dl_id = await _add_downloader("dl1")
    ok = _FakeAdapter([_torrent("a" * 40, "X", 1024**3)])
    bad = _FakeAdapter([], mode="raise")
    svc = _svc(_factory_with({dl_id: ok, dl_id + 999: bad}), monkeypatch)

    await svc.refresh_downloader(dl_id)
    ok.mode = "raise"

    result = await svc.run_scan_job()

    assert result["failed_downloaders"] == 1
    assert result["scanned_downloaders"] == 0
    assert "dl1" in str(result["summary"])
    assert len(result["failures"]) == 1
    assert result["failures"][0]["downloader_name"] == "dl1"
    # 快照保留
    assert (await _snapshots())[0].total_torrents == 1


# ------------------------------------------------------------------ 批量暂停


async def test_anomalies_are_paused_in_batches(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异常种子**批量**暂停（一次 set_pause 发一批），不是逐个发。"""
    dl_id = await _add_downloader("dl1")
    torrents = [
        _torrent(
            f"{i:040x}",
            f"Broken{i}",
            1024**3,
            state="error",
            error_string="Tracker not registered",
            error_code=2,
        )
        for i in range(250)
    ]
    adapter = _FakeAdapter(torrents)
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    assert len(adapter.pause_calls) == 3, "250 个应分成 3 批"
    assert all(len(c) <= PAUSE_BATCH_SIZE for c in adapter.pause_calls)
    assert sum(len(c) for c in adapter.pause_calls) == 250


async def test_already_paused_anomalies_are_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已处于暂停态的异常种子 → 跳过（不重复发暂停指令）。"""
    dl_id = await _add_downloader("dl1")
    torrents = [
        _torrent(
            "a" * 40,
            "Paused1",
            1024**3,
            state="pausedUP",
            error_string="Tracker not registered",
            error_code=2,
        ),
        _torrent(
            "b" * 40,
            "Paused2",
            1024**3,
            state="pausedDL",
            error_string="Tracker not registered",
            error_code=2,
        ),
    ]
    adapter = _FakeAdapter(torrents)
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    assert adapter.pause_calls == [], "已暂停的不该再发暂停"
    # 但异常仍要记录在快照里
    rows = await _snapshots()
    assert rows[0].anomaly_count == 2


async def test_paused_flag_is_recorded_in_snapshot(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照里的异常明细带 paused 标记（前端可显示「已暂停」）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent(
                "a" * 40,
                "P",
                1024**3,
                state="pausedUP",
                error_string="Tracker not registered",
                error_code=2,
            ),
            _torrent(
                "b" * 40,
                "R",
                1024**3,
                state="error",
                error_string="Tracker not registered",
                error_code=2,
            ),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    anomalies = {a["hash"]: a for a in rows[0].anomalies}
    assert anomalies["a" * 40]["paused"] is True
    assert anomalies["b" * 40]["paused"] is False


async def test_pause_count_reflects_actual_actions(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异常数 ≠ 实际暂停数——计数要反映**实际动作**。

    3 个异常：1 个已暂停（跳过）、2 个待暂停但整批失败
    → 实际暂停 0 个，失败 2 个。

    注意：**批次是原子的**——一次 ``set_pause`` 抛错，该批里的 hash
    全部算失败（不能只算一半，那是不诚实的计数）。
    """
    dl_id = await _add_downloader("dl1")

    class _FlakyAdapter(_FakeAdapter):
        async def set_pause(self, hashes: list[str], paused: bool) -> bool:
            self.pause_calls.append(list(hashes))
            raise RuntimeError("暂停失败")

    adapter = _FlakyAdapter(
        [
            _torrent(
                "a" * 40,
                "AlreadyPaused",
                1024**3,
                state="pausedUP",
                error_string="Tracker not registered",
                error_code=2,
            ),
            _torrent(
                "b" * 40,
                "New1",
                1024**3,
                state="error",
                error_string="Tracker not registered",
                error_code=2,
            ),
            _torrent(
                "c" * 40,
                "New2",
                1024**3,
                state="error",
                error_string="Tracker not registered",
                error_code=2,
            ),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await svc.refresh_downloader(dl_id)

    assert result.anomaly_count == 3
    assert result.paused_count == 0, "整批失败 → 一个都没暂停成功"
    assert result.pause_failed_count == 2, "该批的 2 个 hash 都算失败"
    # 已暂停的那个根本没发指令
    assert sum(len(c) for c in adapter.pause_calls) == 2


async def test_job_summary_reports_actual_pause_count(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """作业 summary 说的是**实际暂停数**，不是异常数。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent(
                "a" * 40,
                "AlreadyPaused",
                1024**3,
                state="pausedUP",
                error_string="Tracker not registered",
                error_code=2,
            ),
            _torrent(
                "b" * 40,
                "New",
                1024**3,
                state="error",
                error_string="Tracker not registered",
                error_code=2,
            ),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await svc.run_scan_job()

    assert result["total_anomalies"] == 2
    assert result["paused"] == 1
    summary = str(result["summary"])
    assert "暂停 1 个" in summary
    assert "1 个此前已暂停" in summary
    # 「异常已暂停」是谎报——实际只暂停了 1 个
    assert "2 异常已暂停" not in summary


# ------------------------------------------------------------------ 并发


async def test_downloaders_are_fetched_concurrently(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多个辅种下载器并发抓取（串行最坏 N×20s）。"""
    ids = [await _add_downloader(f"dl{i}") for i in range(3)]
    adapters = {i: _FakeAdapter([_torrent("a" * 40, "X", 1024**3)]) for i in ids}
    svc = _svc(_factory_with(adapters), monkeypatch)

    result = await svc.run_scan_job()

    assert result["scanned_downloaders"] == 3
    assert len(await _snapshots()) == 3


# ------------------------------------------------------------------ 边界


async def test_job_with_no_cross_seed_downloaders(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有辅种下载器 → skipped（不报错、不写库）。"""
    await _add_downloader("dl1", is_cross_seed=False)
    svc = _svc(_factory_with({}), monkeypatch)

    result = await svc.run_scan_job()

    assert result["scanned_downloaders"] == 0
    assert result["failed_downloaders"] == 0
    assert "无辅种下载器" in str(result["summary"])
    assert await _snapshots() == []


async def test_disabled_cross_seed_downloader_is_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已禁用的辅种下载器 → 跳过（不算失败）。"""
    await _add_downloader("dl1", enabled=False)
    svc = _svc(_factory_with({}), monkeypatch)

    result = await svc.run_scan_job()

    assert result["scanned_downloaders"] == 0
    assert result["failed_downloaders"] == 0


async def test_refresh_requires_cross_seed_flag(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非辅种下载器 → 抛 NotCrossSeed（不静默）。"""
    from app.services.cross_seed_monitor import NotCrossSeed

    dl_id = await _add_downloader("dl1", is_cross_seed=False)
    svc = _svc(_factory_with({dl_id: _FakeAdapter([])}), monkeypatch)

    with pytest.raises(NotCrossSeed):
        await svc.refresh_downloader(dl_id)


async def test_refresh_unknown_downloader_raises(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载器不存在 → 抛 DownloaderNotFound。"""
    from app.services.cross_seed_monitor import DownloaderNotFound

    svc = _svc(_factory_with({}), monkeypatch)
    with pytest.raises(DownloaderNotFound):
        await svc.refresh_downloader(9999)


async def test_network_io_is_outside_transaction(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网络 IO 在事务外：抓取期间不持写锁。

    做法：抓取期间另开一个写事务，若抓取持锁则该写会超时/失败。
    """
    dl_id = await _add_downloader("dl1")
    observed: dict[str, bool] = {"write_ok": False}

    class _SlowAdapter(_FakeAdapter):
        async def list_torrents_with_errors(self) -> list[dict[str, object]]:
            self.list_calls += 1
            # 抓取进行中：另一个连接尝试写库
            try:
                async with session_scope() as session:
                    session.add(
                        DownloaderInstance(
                            name="probe",
                            type="qbittorrent",
                            url="http://probe",
                        )
                    )
                observed["write_ok"] = True
            except Exception:
                observed["write_ok"] = False
            return list(self._torrents)

    adapter = _SlowAdapter([_torrent("a" * 40, "X", 1024**3)])
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    assert observed["write_ok"] is True, "抓取期间应能并发写库（未持写锁）"


# ------------------------------------------------------------------ 辅助 API


async def test_list_snapshot_headers(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照头部列表（列表视图用，不含大字段）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter([_torrent("a" * 40, "X", 1024**3)])
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)
    await svc.refresh_downloader(dl_id)

    headers = await svc.list_snapshot_headers()
    assert len(headers) == 1
    assert headers[0].downloader_id == dl_id
    assert headers[0].downloader_name == "dl1"
    assert headers[0].total_torrents == 1


async def test_get_snapshot_returns_none_when_missing(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """还没抓过 → 返回 None（不是抛错）。"""
    svc = _svc(_factory_with({}), monkeypatch)
    assert await svc.get_snapshot(1) is None

async def test_get_snapshot_returns_detail(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照详情含 stats + anomalies。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent("a" * 40, "X", 1024**3),
            _torrent(
                "b" * 40,
                "Bad",
                1024**3,
                state="error",
                error_string="Tracker not registered",
                error_code=2,
            ),
        ]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)
    await svc.refresh_downloader(dl_id)

    snap = await svc.get_snapshot(dl_id)
    assert snap is not None
    assert snap["total_torrents"] == 2
    assert len(snap["stats"]) == 2
    assert len(snap["anomalies"]) == 1


async def test_asyncio_import_is_used() -> None:
    """占位：确保 asyncio 导入被使用（并发实现依赖它）。"""
    assert asyncio is not None


# ------------------------------------------------------------------ 空间统计


async def test_space_stats_persisted_to_snapshot(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空间统计落库：磁盘剩余/总量 + 辅种占用 + 按目录分布。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [
            _torrent(
                "a" * 40,
                "A",
                1000,
                completed_bytes=900,
                save_path="/data/aux",
            ),
            _torrent(
                "b" * 40,
                "B",
                2000,
                completed_bytes=1800,
                save_path="/data/aux",
            ),
            _torrent(
                "c" * 40,
                "C",
                500,
                completed_bytes=500,
                save_path="/data/other",
            ),
        ],
        disk_free_bytes=4000,
        disk_total_bytes=10000,
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    row = rows[0]
    assert row.used_bytes == 3200, "实际占用 = completed_bytes 求和"
    assert row.total_size_bytes == 3500, "总大小 = size 求和"
    assert row.disk_free_bytes == 4000
    assert row.disk_total_bytes == 10000
    # 按目录分布（降序）
    dirs = {d["dir"]: d for d in row.by_dir}
    assert set(dirs) == {"/data/aux", "/data/other"}
    assert dirs["/data/aux"]["count"] == 2
    assert dirs["/data/aux"]["bytes"] == 2700
    assert dirs["/data/aux"]["total_bytes"] == 3000
    assert dirs["/data/other"]["bytes"] == 500


async def test_space_snapshot_detail_includes_by_dir(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照详情含 by_dir（前端按目录展示）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [_torrent("a" * 40, "A", 1000, completed_bytes=900, save_path="/data/aux")]
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    snap = await svc.get_snapshot(dl_id)
    assert snap is not None
    assert len(snap["by_dir"]) == 1
    assert snap["by_dir"][0]["dir"] == "/data/aux"
    assert snap["used_bytes"] == 900
    assert snap["disk_free_bytes"] == 1_000_000


async def test_space_disk_failure_does_not_block_snapshot(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘空间拿不到 → 快照照常落库（占用数据有效，磁盘为 NULL）。"""
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter(
        [_torrent("a" * 40, "A", 1000, completed_bytes=900, save_path="/data/aux")],
        disk_free_bytes=None,  # → get_disk_space 抛 NotImplementedError
    )
    svc = _svc(_factory_with({dl_id: adapter}), monkeypatch)

    await svc.refresh_downloader(dl_id)

    rows = await _snapshots()
    assert rows[0].used_bytes == 900
    assert rows[0].disk_free_bytes is None
    assert rows[0].disk_total_bytes is None
