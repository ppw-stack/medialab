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
from app.adapters.downloaders.base import TorrentFileNames
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

    async def list_torrent_file_names(self) -> list[TorrentFileNames]:
        """孤儿扫描用的顶层名 + downloadDir。"""
        self.list_calls += 1
        if self.fail:
            raise RuntimeError("下载器不可达")
        return [
            TorrentFileNames(
                hash=str(t.get("hash") or ""),
                save_path=str(t.get("save_path") or "") or None,
                top_names=tuple(t.get("top_names") or ()),  # type: ignore[arg-type]
            )
            for t in self._torrents
        ]

    async def close(self) -> None:
        return None


def _torrent(
    top_names: list[str], save_path: str = "/mnt/cross-seed-data/辅种"
) -> dict[str, object]:
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
        # downloadDir —— 孤儿判定按它分组（只认同目录的任务）
        "save_path": save_path,
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


async def _add_non_cross_seed_downloader(name: str = "qb") -> int:
    """加一个**非辅种**下载器（is_cross_seed=False）。

    辅种目录可能被**多个下载器共用**——实测 qB 有任务就在辅种目录里。
    故「有主」集合必须汇总所有下载器，否则共用目录里的数据会被误判
    成孤儿。
    """
    async with session_scope() as session:
        row = DownloaderInstance(
            name=name,
            type="qbittorrent",
            url="http://192.168.0.172:9090",
            is_cross_seed=False,
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
    monkeypatch.setattr(settings, "cross_seed_scan_dirs", str(d))
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
    # 任务的 downloadDir 要与扫描目录**一致**（否则不算「有主」）
    _svc(
        monkeypatch,
        {dl_id: _FakeAdapter([_torrent(["task_dir"], save_path=scan_dir)])},
    )

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
    # 逐目录统计
    assert len(row.scanned_dirs) == 1
    assert row.scanned_dirs[0]["scan_dir"] == scan_dir
    assert row.scanned_dirs[0]["orphan_count"] == 1


async def test_scan_counts_empty_dir_as_orphan(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """**空目录也算孤儿**（0 字节），但单独计数。"""
    root = Path(scan_dir)
    (root / "empty_dir").mkdir()

    dl_id = await _add_downloader("dl1")
    _svc(monkeypatch, {dl_id: _FakeAdapter([])})

    await run_scan_orphans()

    rows = await _rows()
    assert rows[0].orphan_count == 1
    assert rows[0].empty_dir_count == 1
    assert rows[0].orphan_bytes == 0


async def test_scan_multiple_dirs(
    db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多个配置目录 → 各自独立扫描 + 独立统计。"""
    from app.core.config import get_settings

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "orphan_a").mkdir()
    (a / "orphan_a" / "x").write_bytes(b"\0" * 100)
    (b / "owned_b").mkdir()
    (b / "owned_b" / "y").write_bytes(b"\0" * 200)

    monkeypatch.setattr(get_settings(), "cross_seed_scan_dirs", f"{a},{b}")

    dl_id = await _add_downloader("dl1")
    _svc(
        monkeypatch,
        {dl_id: _FakeAdapter([_torrent(["owned_b"], save_path=str(b))])},
    )

    await run_scan_orphans()

    rows = await _rows()
    assert rows[0].orphan_count == 1
    assert rows[0].orphans[0]["name"] == "orphan_a"
    assert len(rows[0].scanned_dirs) == 2


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
        get_settings(), "cross_seed_scan_dirs", "/nonexistent/辅种"
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

    monkeypatch.setattr(get_settings(), "cross_seed_scan_dirs", "")
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


async def test_scan_uses_only_one_downloader_call(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """孤儿扫描**只打一次**下载器接口。

    顶层名与 downloadDir 由 :meth:`list_torrent_file_names` 一并给出，
    不需要再调 :meth:`list_torrents_with_errors`——后者会白拉 TR 的
    ``files`` 字段（辅种站 27.5 MB）。
    """
    dl_id = await _add_downloader("dl1")
    adapter = _FakeAdapter([_torrent(["task_dir"], save_path=scan_dir)])
    _svc(monkeypatch, {dl_id: adapter})

    await run_scan_orphans()

    assert adapter.list_calls == 1, "只该调一次下载器接口"


async def test_orphan_excludes_files_owned_by_other_downloader(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """**其它下载器**（非辅种）认领的条目**不算孤儿**。

    辅种目录可能被多个下载器共用——实测 qB 有 6 个任务的 save_path
    就在辅种目录里。若「有主」只看辅种下载器（TR）的任务，那 6 个
    qB 任务的数据会被误判成孤儿（实测误判 480 GB / 78%）。
    """
    root = Path(scan_dir)
    (root / "tr_owned").mkdir()
    (root / "tr_owned" / "a.mkv").write_bytes(b"\0" * 1000)
    (root / "qb_owned").mkdir()
    (root / "qb_owned" / "b.mkv").write_bytes(b"\0" * 2000)
    (root / "real_orphan").mkdir()
    (root / "real_orphan" / "c.mkv").write_bytes(b"\0" * 3000)

    tr_id = await _add_downloader("tr")
    qb_id = await _add_non_cross_seed_downloader("qb")
    _svc(
        monkeypatch,
        {
            tr_id: _FakeAdapter([_torrent(["tr_owned"], save_path=scan_dir)]),
            qb_id: _FakeAdapter([_torrent(["qb_owned"], save_path=scan_dir)]),
        },
    )

    result = await run_scan_orphans()

    assert result["orphan_count"] == 1, "只有真正无主的才算孤儿"
    rows = await _rows()
    assert rows[0].orphan_bytes == 3000
    names = [o["name"] for o in rows[0].orphans]
    assert names == ["real_orphan"]


async def test_pull_failure_is_reported(
    db: None, monkeypatch: pytest.MonkeyPatch, scan_dir: str
) -> None:
    """某个下载器拉取失败 → **点名**（判定不完整，不能静默）。"""
    tr_id = await _add_downloader("tr")
    qb_id = await _add_non_cross_seed_downloader("qb")
    _svc(
        monkeypatch,
        {
            tr_id: _FakeAdapter([_torrent([], save_path=scan_dir)]),
            qb_id: _FakeAdapter([], fail=True),
        },
    )

    result = await run_scan_orphans()

    # 拉取失败的下载器不贡献「有主」→ 其数据可能被误判 → 必须能看出来
    steps = result["steps"]
    assert any("qb" in str(s) for s in steps), "失败的下载器要点名"
    assert "判定不完整" in result["summary"]


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
