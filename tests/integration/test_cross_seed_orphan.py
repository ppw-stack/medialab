"""辅种孤儿文件统计作业集成测试。

锁住的核心契约：

1. 扫描成功 → 写一行 ``cross_seed_orphan``（数量 + 字节 + 明细）
2. **扫描目录未挂载** → 记 error（**不静默失败**，前端据此显示原因）
3. 下载器拉取失败 → 记 error，**不炸作业**
4. 落库是 **upsert**（同一下载器只一行）
5. 孤儿是**当前快照**语义，不做历史
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.db.models import CrossSeedOrphan, DownloaderInstance
from app.db.session import session_scope
from app.services.cross_seed_orphan import list_orphans, run_scan_orphans
from app.services.factory import AdapterFactory
from sqlalchemy import select


class _FakeAdapter:
    """可控的假下载器适配器。"""

    def __init__(
        self,
        torrents: list[dict[str, object]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self._torrents = torrents or []
        self.fail = fail
        self.list_calls = 0

    async def list_torrents_with_errors(self) -> list[dict[str, object]]:
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("下载器不可达")
        return list(self._torrents)

    async def close(self) -> None:
        return None


def _torrent(top_names: list[str]) -> dict[str, object]:
    return {
        "id": 0,
        "hash": "a" * 40,
        "name": "x",
        "size": 1000,
        "status": 6,
        "error_code": 0,
        "error_string": "",
        "state": "seeding",
        "completed_bytes": 1000,
        "save_path": "/mnt/cross-seed-data/辅种",
        "top_names": top_names,
    }


async def _add_downloader(name: str = "dl1") -> int:
    async with session_scope() as session:
        row = DownloaderInstance(
            name=name,
            type="transmission",
            url="http://192.168.0.172:9091",
            is_cross_seed=True,
            enabled=True,
        )
        session.add(row)
        await session.flush()
        return row.id


def _svc(
    monkeypatch: pytest.MonkeyPatch, adapters: dict[int, _FakeAdapter]
) -> None:
    """把全局工厂换成假工厂。"""
    import app.services.factory as factory_mod

    factory = AdapterFactory()

    def _downloader(row: DownloaderInstance, **kw: object) -> _FakeAdapter:
        return adapters[row.id]

    factory.downloader = _downloader  # type: ignore[method-assign]
    monkeypatch.setattr(factory_mod, "_factory", factory)


async def _rows() -> list[CrossSeedOrphan]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(CrossSeedOrphan).order_by(CrossSeedOrphan.downloader_id)
                )
            ).scalars()
        )


@pytest.fixture
def scan_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """把扫描目录指向临时目录，并让 settings 里的路径指过去。"""
    from app.core.config import get_settings

    d = tmp_path / "辅种"
    d.mkdir()
    settings = get_settings()
    monkeypatch.setattr(settings, "cross_seed_scan_dir", str(d))
    return str(d)


# ------------------------------------------------------------------ 正常


async def test_scan_writes_orphan_row(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """扫描成功 → 写一行（数量 + 字节 + 明细）。"""
    root = Path(scan_dir)
    (root / "orphan_dir").mkdir()
    (root / "orphan_dir" / "a.mkv").write_bytes(b"\0" * 1000)
    (root / "task_dir").mkdir()
    (root / "task_dir" / "b.mkv").write_bytes(b"\0" * 2000)

    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([_torrent(["task_dir"])])})

    result = await run_scan_orphans()

    assert result["scanned_downloaders"] == 1
    assert result["orphan_count"] == 1
    assert result["orphan_bytes"] == 1000

    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.downloader_id == dl_id
    assert row.downloader_name == "dl1"
    assert row.orphan_count == 1
    assert row.orphan_bytes == 1000
    assert row.total_scan_bytes == 3000
    assert row.total_scan_count == 2
    assert row.error is None
    assert len(row.orphans) == 1
    assert row.orphans[0]["name"] == "orphan_dir"


async def test_scan_is_upsert(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """第二次扫描**覆盖**同一行（当前快照语义，不堆行）。"""
    root = Path(scan_dir)
    (root / "o1").mkdir()
    (root / "o1" / "a").write_bytes(b"\0" * 100)

    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([_torrent([])])})

    await run_scan_orphans()
    (root / "o2").mkdir()
    (root / "o2" / "b").write_bytes(b"\0" * 200)
    await run_scan_orphans()

    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].orphan_count == 2


# ------------------------------------------------------------------ 降级


async def test_scan_dir_missing_records_error(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描目录不存在 → 记 error（**不抛**，作业不炸）。"""
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "cross_seed_scan_dir", "/nonexistent/辅种"
    )
    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([_torrent([])])})

    result = await run_scan_orphans()

    assert result["scanned_downloaders"] == 1
    assert result["orphan_count"] == 0
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].error is not None
    assert "不存在" in rows[0].error


async def test_scan_dir_not_configured_records_error(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置扫描目录 → 记 error（前端显示「未启用」）。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "cross_seed_scan_dir", "")
    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([_torrent([])])})

    await run_scan_orphans()

    rows = await _rows()
    assert rows[0].error is not None
    assert "未配置" in rows[0].error


async def test_downloader_failure_records_error(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """下载器拉取失败 → 记 error，**作业不炸**。"""
    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([], fail=True)})

    result = await run_scan_orphans()

    assert result["scanned_downloaders"] == 1
    assert result["orphan_count"] == 0
    rows = await _rows()
    assert rows[0].error is not None


async def test_no_cross_seed_downloaders(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有辅种下载器 → skipped（不写库）。"""
    _svc(monkeypatch, {})

    result = await run_scan_orphans()

    assert result["scanned_downloaders"] == 0
    assert await _rows() == []


# ------------------------------------------------------------------ 查询


async def test_list_orphans(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """查询接口返回落库的统计。"""
    root = Path(scan_dir)
    (root / "o").mkdir()
    (root / "o" / "a").write_bytes(b"\0" * 500)

    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([_torrent([])])})
    await run_scan_orphans()

    rows = await list_orphans()
    assert len(rows) == 1
    assert rows[0].downloader_name == "dl1"
    assert rows[0].orphan_count == 1
    assert rows[0].orphan_bytes == 500
    assert rows[0].to_dict()["orphans"][0]["name"] == "o"
