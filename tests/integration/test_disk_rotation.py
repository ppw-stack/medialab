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
    scanned_at: str | None = None,
) -> None:
    """直接写一行 cross_seed_snapshot.

    ``scanned_at`` 不传 = ORM 默认（当前北京时间，即"新鲜"）；
    传旧时间可测「快照过期 → 跳过删除」。
    """
    async with session_scope() as session:
        row = CrossSeedSnapshot(
            downloader_id=dl_id,
            downloader_name=dl_name,
            total_torrents=sum(int(s.get("count") or 0) for s in stats),
            unique_count=len(stats),
            disk_free_bytes=disk_free,
            disk_total_bytes=disk_total,
            stats=stats,
            **({"scanned_at": scanned_at} if scanned_at else {}),
        )
        session.add(row)


def _stat(
    name: str,
    size_gb: float,
    count: int,
    hashes: list[str],
    *,
    added_at: dict[str, int] | None = None,
    size_bytes: int | None = None,
    blocked_hashes: list[str] | None = None,
) -> dict[str, object]:
    """构造一条 stats 项.

    hashes 必须是 40 位完整 hex（删除要用）。
    added_at: hash → unix 秒映射（可选；disk_rotation 同档先删旧的用）。
    size_bytes: 精确字节数（可选；缺失时由 size_gb 推算，释放量断言用）。
    blocked_hashes: 组内冻结 hash 列表（可选；组内成员正在校验/下载 → 非空，disk_rotation 整组本轮跳过）。
    """
    payload: dict[str, object] = {
        "name": name,
        "size_gb": size_gb,
        "count": count,
        "sample_hash": hashes[0][:12] if hashes else "",
        "hashes": hashes,
        "added_at": added_at or {},
    }
    if size_bytes is not None:
        payload["size_bytes"] = size_bytes
    if blocked_hashes:
        payload["blocked_hashes"] = blocked_hashes
    return payload


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


# ------------------------------------------------------------------ 11. 同档先删旧的
async def test_same_bucket_deletes_oldest_first(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同档（count 相同）内**先删加入更早的**（2026-10-28 加入）.

    构造（total=100GB used=99GB → 需释放 9GB）：
    - 独苗 A：5 GB，added_at=100（**早**）→ 应**先**删
    - 独苗 B：5 GB，added_at=200（晚）
    - 独苗 C：10 GB，added_at=300（最晚，最大）→ 旧规则会先删它（大优先），
      新规则先删 A（旧优先）

    需释放 9GB：删 A(5) 不够 → 还要删 B(5) 共 10GB 才达标。
    若按旧规则（size 降序）会先删 C(10) 一次达标——本测试就是要锁住
    「同档内 added_at 优先于 size」这个新行为。
    """
    stats = [
        _stat("Solo.A", 5.0, 1, [_hash("a", 1)], added_at={_hash("a", 1): 100}),
        _stat("Solo.B", 5.0, 1, [_hash("b", 1)], added_at={_hash("b", 1): 200}),
        _stat("Solo.C", 10.0, 1, [_hash("c", 1)], added_at={_hash("c", 1): 300}),
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

    # 删 A(5) + B(5) = 10GB ≥ 9GB 需求；C 不该被删（虽然它最大）
    assert result["deleted"] == 2
    removed_hashes = [h for batch in adapter.removed for h in batch[0]]
    assert _hash("a", 1) in removed_hashes, "应先删早加入的 A"
    assert _hash("b", 1) in removed_hashes, "A 不够再删 B"
    assert _hash("c", 1) not in removed_hashes, "C（最大但最新）不应被删"


# ------------------------------------------------------------------ 12. 缺失时间不当旧
async def test_missing_added_at_not_treated_as_oldest(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺失 ``added_at`` 的种子排在有时间的**之后**（不当成最旧）.

    构造（total=100GB used=99GB → 需释放 9GB）：
    - 独苗 A：5 GB，added_at 缺失（无时间档）
    - 独苗 B：10 GB，added_at=100（有时间，最早）
    - 独苗 C：10 GB，added_at=200（有时间，较晚）

    需释放 9GB：按新规则先删 B(10) 一次达标。
    若「缺失当成最旧」会先删 A(5) → 不够 → 再删 B(10) 共 15GB，
    这是**错误**行为，本测试要锁住降级规则。
    """
    stats = [
        _stat("Solo.A", 5.0, 1, [_hash("a", 1)]),  # 无 added_at
        _stat("Solo.B", 10.0, 1, [_hash("b", 1)], added_at={_hash("b", 1): 100}),
        _stat("Solo.C", 10.0, 1, [_hash("c", 1)], added_at={_hash("c", 1): 200}),
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

    # 删 B(10) 一次达标；A（无时间）与 C 都不该被删
    assert result["deleted"] == 1
    assert adapter.removed[0][0] == [_hash("b", 1)], "应删有时间的最早者 B"


# ------------------------------------------------------------------ 13. 旧快照降级
async def test_old_snapshot_without_added_at_falls_back(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧快照 stats 项没有 ``added_at`` 字段 → 排序退化为原行为.

    构造（total=100GB used=99GB → 需释放 9GB）：
    - 独苗 A：5 GB（无 added_at 字段）
    - 独苗 B：10 GB（无 added_at 字段）

    旧规则：size 降序 → 先删 B(10) 一次达标。
    降级后行为应与旧规则**完全一致**——锁住「不改变现有行为」。
    """
    stats: list[dict[str, object]] = [
        # 旧快照形状：没有 added_at key
        {
            "name": "Solo.A",
            "size_gb": 5.0,
            "count": 1,
            "sample_hash": _hash("a", 1)[:12],
            "hashes": [_hash("a", 1)],
        },
        {
            "name": "Solo.B",
            "size_gb": 10.0,
            "count": 1,
            "sample_hash": _hash("b", 1)[:12],
            "hashes": [_hash("b", 1)],
        },
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

    # 降级到 size 降序 → 先删 B(10) 一次达标
    assert result["deleted"] == 1
    assert adapter.removed[0][0] == [_hash("b", 1)], "降级后应删大种子 B"


# ------------------------------------------------------------------ 14. 档位优先不被打破
async def test_bucket_priority_preserved_with_added_at(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """档位（count）优先于 added_at —— ``count=1`` 档内再新，也排在 ``count=3`` 档之前.

    构造（total=100GB used=99GB → 需释放 9GB）：
    - 独苗 A：10 GB，added_at=999（很新）→ count=1 档，应**先**删
    - 多副本 B：10 GB，count=3，added_at=1（很旧）→ 多副本保留

    即使 B 比 A 旧得多，count 升序是主排序，独苗优先删。
    """
    stats = [
        _stat("Solo.A", 10.0, 1, [_hash("a", 1)], added_at={_hash("a", 1): 999}),
        _stat(
            "Multi.B", 10.0, 3,
            [_hash("b", 1), _hash("b", 2), _hash("b", 3)],
            added_at={_hash("b", 1): 1, _hash("b", 2): 1, _hash("b", 3): 1},
        ),
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

    # 删独苗 A(10) 一次达标；多副本 B 不该被删
    assert result["deleted"] == 1
    assert adapter.removed[0][0] == [_hash("a", 1)], "档位优先：独苗先删"


# ------------------------------------------------------------------ 15. 整组删（原子）
async def test_multi_copy_group_deleted_atomically(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多副本组必须**整组**一次删完，不许只删其中几个.

    为什么要这样：同 ``(name, size)`` 的多个 infohash 指向**同一份磁盘数据**
    （实测 4646 个种子 → 2237 份数据）。只删一个成员 → 一个字节都不释放；
    删数据模式下删一个成员 → 共用文件被删，余下副本立刻 ``missingFiles``。

    构造：total=100GB free=1GB → 需释放 89GB，候选只有 5GB + 10GB → 全删。
    批量上限压到 2，让「1 个独苗 + 3 副本组」必须分成两批——锁住
    「组不被切进两个批次」。
    """
    import app.services.disk_rotation as dr

    monkeypatch.setattr(dr, "DELETE_BATCH_SIZE", 2)

    multi = [_hash("m", 1), _hash("m", 2), _hash("m", 3)]
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[
            _stat("Solo.A", 5.0, 1, [_hash("a", 1)]),
            _stat("Multi.B", 10.0, 3, multi),
        ],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["deleted_groups"] == 2
    assert result["deleted"] == 4, "1 个独苗 + 3 个副本全删"
    # 3 个副本在**同一次** remove 里（组没被拆开）
    assert any(set(hashes) == set(multi) for hashes, _ in adapter.removed), (
        f"多副本组应整组一次删：{adapter.removed}"
    )


# ------------------------------------------------------------------ 16. 释放上界 / 不可达
async def test_release_is_capped_by_releasable_data(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """需释放量 > 辅种可释放量 → 只删能释放的，并标「目标不可达」.

    分区被非辅种数据占满时（媒体库 / 备份），辅种全删光也补不上差额。
    旧实现会把候选全删并把"需求"当释放量记账（幽灵数字）；
    现在按 `min(need, max_releasable)` 收敛，并在结果里点名不可达。
    """
    size = 1 * 1024**3  # 1 GiB（需释放 9 GiB → 远远不够，构出"不可达"）
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[
            _stat("Solo.A", 1.0, 1, [_hash("a", 1)], size_bytes=size),
        ],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["unreachable_downloaders"] == 1
    assert "不可达" in result["summary"]
    assert result["deleted"] == 1
    assert result["freed_bytes"] == size, "释放量只按真实删掉的数据记，不按需求记"
    # 结果里保留**原始需求**（便于人工判断缺多少：need - max_releasable）
    row = result["results"][0]
    assert row["need_free_bytes"] > row["max_releasable_bytes"]
    assert row["max_releasable_bytes"] == size
    assert "缺" in result["summary"]


# ------------------------------------------------------------------ 17. 预演模式
async def test_dry_run_only_plans(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``disk_rotation_dry_run=1`` → 只算计划，不发任何删除指令."""
    mgr = RuntimeParamManager()
    mgr.set_memory("disk_rotation_threshold_pct", 90)
    mgr.set_memory("disk_rotation_action", 1)
    mgr.set_memory("disk_rotation_dry_run", 1)
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    mgr.set_memory("monitor_delete_expired", 0)
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_download_interval_ms", 0)
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

    assert result["dry_run"] is True
    assert result["planned_groups"] == 1
    assert result["planned_torrents"] == 1
    assert result["deleted"] == 0
    assert adapter.removed == [], "预演不许发删除指令"
    assert "[预演]" in result["summary"]


# ------------------------------------------------------------------ 18. 快照过期
async def test_stale_snapshot_skips_deletion(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照超过 6 小时 → 跳过删除（不可逆动作不赌陈旧判断）.

    ① 巡查失败时**不写**快照：连续失败几天的话，② 手里的 hash 列表
    可能完全过时（期间种子早就换过好几轮）。
    """
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[_stat("Solo.A", 10.0, 1, [_hash("a", 1)])],
        scanned_at="2026-11-01 03:17:00",  # 20 天前
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert result["triggered_downloaders"] == 0
    assert adapter.removed == [], "过期快照不许删任何东西"
    errs = [r.get("error") or "" for r in result["results"]]
    assert any("过期" in e for e in errs)


# ------------------------------------------------------------------ 19. 本轮抓取失败
async def test_skip_downloader_ids_respected(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """① 本轮抓取失败的下载器 → ② 跳过（快照没更新，按旧数据删不许）."""
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

    result = await run_disk_rotation(skip_downloader_ids=frozenset({dl_id}))

    assert adapter.removed == []
    errs = [r.get("error") or "" for r in result["results"]]
    assert any("巡查抓取失败" in e for e in errs)


# ------------------------------------------------------------------ 20. 校验/下载中整组跳过
async def test_group_with_checking_member_is_skipped(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组内有成员在**校验中** → 整组本轮跳过，改删下一组.

    删组是整份数据的原子操作：组内有人正在 recheck 时删它，完成度判定不可靠，
    qB/TR 还可能重建文件（AGENTS：校验中的种子绝不删除）。
    """
    frozen = _hash("f", 1)
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[
            # 独苗，但正在校验 → 跳过
            _stat("Frozen.A", 20.0, 1, [frozen], blocked_hashes=[frozen]),
            # 另一个独苗，正常 → 删它
            _stat("Solo.B", 10.0, 1, [_hash("b", 1)]),
        ],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    removed = [h for batch in adapter.removed for h in batch[0]]
    assert frozen not in removed, "校验中的组不许删"
    assert _hash("b", 1) in removed
    assert result["blocked_groups"] == 1
    assert "校验/下载" in result["summary"]


# ------------------------------------------------------------------ 21. 全部冻结
async def test_all_groups_blocked_reports_reason(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选全被冻结 → 一个都不删，且原因写清是「校验/下载」而不是「没候选」."""
    frozen = _hash("f", 1)
    dl_id = await _add_downloader("dl1")
    await _add_snapshot(
        dl_id,
        "dl1",
        disk_free=1 * 1024**3,
        disk_total=100 * 1024**3,
        stats=[
            _stat("Frozen.A", 20.0, 1, [frozen], blocked_hashes=[frozen]),
        ],
    )
    adapter = _FakeAdapter()
    _svc(_factory_with({dl_id: adapter}), monkeypatch)

    result = await run_disk_rotation()

    assert adapter.removed == []
    assert result["blocked_groups"] == 1
    err = result["results"][0].get("error") or ""
    assert "校验/下载" in err, err
