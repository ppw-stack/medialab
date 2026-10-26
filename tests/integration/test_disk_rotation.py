"""磁盘空间流转作业集成测试.

**锁住的核心契约**（2026-10-26）：

辅种下载器磁盘使用率超阈时，按「价值低→高」删除种子，把使用率
压回阈值以下。价值口径用 ``(原始 name, size_bytes)`` 分组的
``count`` —— 生产 tr-172 实测验证：count=13 的组里 13 个 infohash
各不相同（各站 announce URL 不同），但 name+size 完全一致 → 是
同一片源的 13 个辅种副本。count=1 是独苗，价值最低，优先删。

测试场景：
1. 使用率不超阈 → skipped
2. 超阈 → 选独苗优先删 + 大种子优先
3. qB 拿不到 disk_total → 跳过（不报错）
4. 删磁盘数据走审计日志 + 批量删除
5. 单批失败不中断后续批次
6. 候选选择排序：count 升序 + size 降序
"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import DownloaderError
from app.db.models import CrossSeedSnapshot, DownloaderInstance
from app.db.session import session_scope
from app.services.disk_rotation import run_disk_rotation
from app.services.factory import AdapterFactory
from app.services.runtime_params import (
    RuntimeParamManager,
    set_runtime_params,
)


# ------------------------------------------------------------------ 辅助
class _FakeAdapter:
    """记录 remove 调用的假辅种下载器适配器."""

    def __init__(
        self,
        *,
        remove_fail_hashes: set[str] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        #: 会让 remove 失败的 hash 集合（用于测单批失败）
        self._remove_fail_hashes = remove_fail_hashes or set()
        self._fail_with = fail_with
        #: 每次 remove 调用的 (hashes, delete_data)
        self.removed: list[tuple[list[str], bool]] = []

    async def remove(self, hashes: list[str], *, delete_data: bool) -> bool:
        self.removed.append((list(hashes), delete_data))
        if self._fail_with is not None:
            raise self._fail_with
        # 没有失败 hash 时直接返回成功
        return True

    async def close(self) -> None:
        return None


async def _add_downloader(
    name: str = "dl1", *, enabled: bool = True
) -> int:
    async with session_scope() as session:
        row = DownloaderInstance(
            name=name,
            type="transmission",
            url="http://localhost:9091",
            is_cross_seed=True,
            enabled=enabled,
        )
        session.add(row)
        await session.flush()
        return row.id


async def _add_snapshot(
    dl_id: int,
    dl_name: str,
    *,
    disk_free: int | None,
    disk_total: int | None,
    stats: list[dict[str, object]],
) -> None:
    """直接写一行 cross_seed_snapshot."""
    async with session_scope() as session:
        row = CrossSeedSnapshot(
            downloader_id=dl_id,
            downloader_name=dl_name,
            total_torrents=sum(int(s.get("count") or 0) for s in stats),
            unique_count=len(stats),
            disk_free_bytes=disk_free,
            disk_total_bytes=disk_total,
            stats=stats,
        )
        session.add(row)


def _stat(
    name: str,
    size_gb: float,
    count: int,
    hashes: list[str],
) -> dict[str, object]:
    """构造一条 stats 项.

    hashes 必须是 40 位完整 hex（删除要用）。
    """
    return {
        "name": name,
        "size_gb": size_gb,
        "count": count,
        "sample_hash": hashes[0][:12] if hashes else "",
        "hashes": hashes,
    }


def _hash(prefix: str, idx: int) -> str:
    """构造 40 位 hex hash（前缀 + idx 填充）."""
    s = f"{prefix}{idx:04d}"
    return (s + "0" * 40)[:40]


@pytest.fixture(autouse=True)
def _runtime_params() -> None:
    """注入运行参数：阈值 90% + 删任务+删数据."""
    mgr = RuntimeParamManager()
    mgr.set_memory("disk_rotation_threshold_pct", 90)
    mgr.set_memory("disk_rotation_action", 1)
    # 兼容 monitor 等作业的 spec（_assert_enum_options_consistent 导入时已校验）
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("monitor_delete_expired", 0)
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_download_interval_ms", 0)
    mgr.set_memory("push_within_bucket_order", 0)
    set_runtime_params(mgr)
    yield
    set_runtime_params(None)  # type: ignore[arg-type]


def _factory_with(adapters: dict[int, _FakeAdapter]) -> AdapterFactory:
    factory = AdapterFactory()

    def _downloader(row: DownloaderInstance, **kw: object) -> _FakeAdapter:
        return adapters[row.id]

    factory.downloader = _downloader  # type: ignore[method-assign]
    return factory


def _svc(factory: AdapterFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """把全局工厂换成假工厂（disk_rotation 直接用全局 factory）."""
    import app.services.factory as factory_mod

    monkeypatch.setattr(factory_mod, "_factory", factory)


# ------------------------------------------------------------------ 1. 不超阈
async def test_under_threshold_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """使用率 80% < 阈值 90% → 不触发流转."""
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        # total=100GB, used=80GB → 80%
        disk_free=20 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[_stat("Movie.A", 1.0, 1, [_hash("a", 1)])],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["triggered_downloaders"] == 0
    assert result["deleted"] == 0
    assert adapter.removed == [], "未触发不应调 remove"


# ------------------------------------------------------------------ 2. 超阈→删独苗
async def test_over_threshold_deletes_low_value(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈 → 优先删 count=1 独苗，同档内大种子优先.

    构造：
    - total=100GB, free=5GB → used=95GB > 90% 阈值
    - 需释放 = 95 - 90 = 5GB
    - 候选：
      * 独苗 A，10 GB（应优先删——大种子 + 独苗）
      * 独苗 B，3 GB（应优先删——独苗但小，A 先删够 5GB 就停）
      * 多副本 C，count=3，1 GB（不应删——多副本保留）
    """
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=5 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[
            _stat("Solo.A", 10.0, 1, [_hash("a", 1)]),
            _stat("Solo.B", 3.0, 1, [_hash("b", 1)]),
            _stat("Multi.C", 1.0, 3, [_hash("c", 1), _hash("c", 2), _hash("c", 3)]),
        ],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["triggered_downloaders"] == 1
    assert result["deleted"] == 1, "只需删 1 个 10GB 独苗就够释放 5GB"
    assert adapter.removed[0][0] == [_hash("a", 1)], "应删独苗 A（10GB）"
    assert adapter.removed[0][1] is True, "delete_data 应为 True（删数据模式）"


# ------------------------------------------------------------------ 3. qB 无 total
async def test_no_disk_total_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qB 拿不到 disk_total_bytes → 跳过（不报错）."""
    dl_id = await _add_downloader("qb1")
    await _add_snapshot(
        dl_id,
        "qb1",
        disk_free=5 * 1024**3,
        disk_total=None,  # qB 不提供
        stats=[_stat("Solo.A", 10.0, 1, [_hash("a", 1)])],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["triggered_downloaders"] == 0
    assert adapter.removed == []
    # 该下载器计 skipped_no_total，不算失败
    assert result["failures"] == []


# ------------------------------------------------------------------ 4. 批量删除
async def test_batch_delete_split(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选超 DELETE_BATCH_SIZE → 分批删除.

    构造：阈值 90%，total=100GB used=99GB → 需释放 9GB
    候选：4 个独苗各 3 GB → 删前 3 个共 9GB 即满足（不够 → 删第 4 个）
    为了确保**4 个都删**（才能看到 2 批 = 2+2），把需求设为 12 GB：
    total=100GB, used=99.99GB（free=10MB → ~99.99%）→ 需释放约 9.99GB
    改用更直白的：阈值 50% → used=99GB / total=100 → 需释放 49GB → 4 个独苗 3GB 共 12GB 全删仍不够
    """
    import app.services.disk_rotation as dr

    monkeypatch.setattr(dr, "DELETE_BATCH_SIZE", 2)

    mgr = RuntimeParamManager()
    mgr.set_memory("disk_rotation_threshold_pct", 0)
    mgr.set_memory("disk_rotation_action", 1)
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("monitor_delete_expired", 0)
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_download_interval_ms", 0)
    mgr.set_memory("push_within_bucket_order", 0)
    set_runtime_params(mgr)

    stats = [
        _stat(f"Solo.{i}", 3.0, 1, [_hash("s", i)]) for i in range(4)
    ]
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=stats,
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    # 4 个独苗都删（need=99GB > 候选 12GB → 全删）
    assert result["deleted"] == 4
    # 4 个 / 批量 2 → 2 批
    assert len(adapter.removed) == 2
    assert len(adapter.removed[0][0]) == 2
    assert len(adapter.removed[1][0]) == 2


# ------------------------------------------------------------------ 5. 单批失败不中断
async def test_batch_failure_does_not_stop(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单批 remove 抛 DownloaderError → 计 failed，继续删下一批.

    阈值=0 让所有候选都删（避免被「释放达标就停」截断到 3 个）。
    """
    import app.services.disk_rotation as dr

    monkeypatch.setattr(dr, "DELETE_BATCH_SIZE", 1)

    mgr = RuntimeParamManager()
    mgr.set_memory("disk_rotation_threshold_pct", 0)
    mgr.set_memory("disk_rotation_action", 1)
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("monitor_delete_expired", 0)
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_download_interval_ms", 0)
    mgr.set_memory("push_within_bucket_order", 0)
    set_runtime_params(mgr)

    # 4 个独苗各 3 GB
    stats = [
        _stat(f"Solo.{i}", 3.0, 1, [_hash("s", i)]) for i in range(4)
    ]
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=stats,
    )

    # 第 1 个 hash 失败，其余成功
    failing = {_hash("s", 0)}

    class _PartialAdapter(_FakeAdapter):
        async def remove(self, hashes: list[str], *, delete_data: bool) -> bool:
            self.removed.append((list(hashes), delete_data))
            if failing.intersection(hashes):
                raise DownloaderError("连接失败")
            return True

    adapter = _PartialAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    # 4 次调用（每批 1 个），1 个失败 3 个成功
    assert len(adapter.removed) == 4
    assert result["deleted"] == 3
    assert result["delete_failed"] == 1


# ------------------------------------------------------------------ 6. 候选排序
async def test_candidate_ordering(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选排序：count 升序（独苗优先）→ size 降序（大种子优先）.

    构造（total=100GB used=99GB → 需释放 9GB）：
    - 独苗 1：5 GB（count=1）
    - 独苗 2：10 GB（count=1，比独苗 1 大 → 应**先**删）
    - 独苗 3：3 GB（count=1）
    - 多副本 A：6 GB，count=2（多副本，**不应**删）
    """
    stats = [
        _stat("Solo.Small", 5.0, 1, [_hash("a", 1)]),
        _stat("Solo.Big", 10.0, 1, [_hash("b", 1)]),
        _stat("Solo.Mid", 3.0, 1, [_hash("c", 1)]),
        _stat("Multi.A", 6.0, 2, [_hash("d", 1), _hash("d", 2)]),
    ]
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=stats,
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    # 删独苗 10 GB（先）→ 释放 10 GB 已 ≥ 9 GB 需求，停
    assert result["deleted"] == 1
    assert adapter.removed[0][0] == [_hash("b", 1)], "应先删大独苗"


# ------------------------------------------------------------------ 7. 无辅种下载器
async def test_no_snapshot_skip(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无辅种快照 → 不报错，summary 写"无辅种下载器快照"."""
    _svc(_factory_with({}), monkeypatch)

    result = await run_disk_rotation()

    assert result["scanned_downloaders"] == 0
    assert result["deleted"] == 0
    assert "无辅种下载器快照" in result["summary"]


# ------------------------------------------------------------------ 8. 下载器已禁用
async def test_disabled_downloader_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有快照但下载器已禁用 → 跳过该下载器."""
    dl_id = await _add_downloader("dl1", enabled=False)
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[_stat("Solo.A", 10.0, 1, [_hash("a", 1)])],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["triggered_downloaders"] == 0
    assert adapter.removed == []


# ------------------------------------------------------------------ 9. 仅删任务模式
async def test_delete_data_false(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """disk_rotation_action=0 → delete_data=False（仅删任务）."""
    mgr = RuntimeParamManager()
    mgr.set_memory("disk_rotation_threshold_pct", 90)
    mgr.set_memory("disk_rotation_action", 0)
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("monitor_delete_expired", 0)
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_download_interval_ms", 0)
    mgr.set_memory("push_within_bucket_order", 0)
    set_runtime_params(mgr)

    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[_stat("Solo.A", 10.0, 1, [_hash("a", 1)])],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["deleted"] == 1
    assert adapter.removed[0][1] is False, "delete_data 应为 False"


# ------------------------------------------------------------------ 10. 旧快照无 hashes
async def test_old_snapshot_without_hashes_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧快照的 stats 项没有 hashes 字段 → 该组跳过（不报错）."""
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[
            # 旧快照：只有 sample_hash（12 位截断），无 hashes
            {"name": "Solo.A", "size_gb": 10.0, "count": 1, "sample_hash": "aabbccddeeff"},
        ],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    # 触发但无候选 → 错误标记"无候选种子"
    assert result["triggered_downloaders"] == 1
    assert result["deleted"] == 0
    assert adapter.removed == []
