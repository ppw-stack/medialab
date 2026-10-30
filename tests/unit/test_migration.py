"""迁移引擎（115 → 光鸭）的契约测试。

覆盖的关键行为：

1. 阈值 / 后缀 / 原盘过滤
2. 目标目录预检：同名同大小 → 跳过；同名不同大小 → 冲突（不覆盖、不重传）
3. 上一轮在途任务（按 pickcode 匹配）→ 跳过，不重复提交
4. 提交 → 等任务消失 → 校验落盘 → 成功
5. 相对目录映射（剥离源目录前缀）
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from app.adapters.drivers.base import CloudFile, DirectLink
from app.db.models import Driver
from app.services import migration, migration_jobs

# --------------------------------------------------------------------------
# 假驱动
# --------------------------------------------------------------------------


class _FakeP115:
    """按 cid 遍历的假 115：cid = 根路径。"""

    name = "p115"

    def __init__(self, files: list[CloudFile]) -> None:
        self._files = files
        self.links: list[tuple[str, str]] = []

    async def iter_tree_by_cid(
        self, cid: str, parent_path: str = "", include_subdirs: bool = True
    ) -> AsyncIterator[CloudFile]:
        for entry in self._files:
            yield entry

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        self.links.append((remote_id, user_agent))
        return DirectLink(url=f"fake://{remote_id}", ua_required=True)


class _FakeGuangya:
    """内存版光鸭：目录树 + 离线任务队列。"""

    name = "guangya"

    def __init__(self, info: dict[str, tuple[str, int]]) -> None:
        #: pickcode → (文件名, 大小)——提交任务后"落盘"用
        self._info = info
        #: parent_id → {目录名: fileId}
        self._dirs: dict[str, dict[str, str]] = {"root": {}}
        #: fileId → {文件名: 大小}
        self._files: dict[str, dict[str, int]] = {"root": {}}
        self._next_id = 0
        self._submitted: set[str] = set()
        self._legacy: list[dict[str, Any]] = []
        self.created_dirs: list[str] = []
        self.submitted: list[tuple[str, str]] = []
        #: 每次提交的时刻（用于断言「错开提交」）
        self.submit_times: list[float] = []

    # -- 目录 --

    def seed_dir(self, rel: str) -> str:
        """预建一个目录（相对根，``a/b`` 形式）。"""
        parent = "root"
        for name in [p for p in rel.split("/") if p]:
            parent = self._ensure(name, parent)
        return parent

    def seed_file(self, rel_dir: str, name: str, size: int) -> None:
        dir_id = self.seed_dir(rel_dir)
        self._files[dir_id][name] = size

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        parent = parent_id or "root"
        existing = self._dirs.setdefault(parent, {}).get(name)
        if existing is not None:
            return existing, False
        self._next_id += 1
        dir_id = f"dir{self._next_id}"
        self._dirs[parent][name] = dir_id
        self._dirs[dir_id] = {}
        self._files[dir_id] = {}
        self.created_dirs.append(f"{parent}/{name}")
        return dir_id, True

    def _ensure(self, name: str, parent: str) -> str:
        existing = self._dirs.setdefault(parent, {}).get(name)
        if existing is not None:
            return existing
        self._next_id += 1
        dir_id = f"dir{self._next_id}"
        self._dirs[parent][name] = dir_id
        self._dirs[dir_id] = {}
        self._files[dir_id] = {}
        return dir_id

    async def list_files(self, dir_id: str) -> dict[str, int]:
        return dict(self._files.get(dir_id or "root", {}))

    # -- 离线任务 --

    async def create_offline_task(self, url: str, parent_id: str = "") -> str:
        pickcode = url.removeprefix("fake://")
        name, size = self._info[pickcode]
        self._files.setdefault(parent_id or "root", {})[name] = size
        task_id = f"task-{pickcode}"
        self._submitted.add(task_id)
        self.submitted.append((pickcode, parent_id))
        self.submit_times.append(time.monotonic())
        return task_id

    async def list_active_offline_tasks(self) -> list[dict[str, Any]]:
        # 上一轮遗留的任务永远在跑（本轮应跳过它对应的文件）
        items = list(self._legacy)
        for task_id in list(self._submitted):
            items.append({"taskId": task_id, "res": "https://cdn/x"})
            self._submitted.remove(task_id)  # 下一次轮询就"消失"
        return items


# --------------------------------------------------------------------------
# 固件
# --------------------------------------------------------------------------


def _entry(path: str, size: int, pickcode: str) -> CloudFile:
    return CloudFile(
        remote_id=pickcode,
        path=path,
        name=path.rsplit("/", 1)[-1],
        size=size,
        is_dir=False,
    )


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch):
    """把轮询间隔压到 10ms，避免测试等 15 秒。"""
    monkeypatch.setattr(migration, "POLL_INTERVAL_SEC", 0.01)


async def _seed_drivers(db_session) -> None:
    db_session.add(
        Driver(
            id=101,
            type="p115",
            name="115",
            credential=None,
            enabled=True,
            video_exts="mkv,mp4",
            created_at=int(time.time()),
        )
    )
    db_session.add(
        Driver(
            id=102,
            type="guangya",
            name="光鸭",
            credential=None,
            enabled=True,
            created_at=int(time.time()),
        )
    )
    await db_session.commit()


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------


def test_relative_dir_strips_source_prefix() -> None:
    assert (
        migration._relative_dir(
            "/影视/电影/动画电影/龙猫 (1988)/龙猫.mkv", "/影视/电影/动画电影"
        )
        == "龙猫 (1988)"
    )
    assert migration._relative_dir("/影视/电影/x/a.mkv", "/影视") == "电影/x"
    assert migration._relative_dir("/影视/电影/x/a.mkv", "/other") == ""


def test_filter_threshold_and_disc() -> None:
    mkv = _entry("/影视/电影/x/a.mkv", 10 * 1024 * 1024, "p1")
    assert migration._filter(
        mkv, video_exts={"mkv"}, min_bytes=1024, max_bytes=0, exclude_disc=True
    ) is None
    assert (
        migration._filter(
            mkv, video_exts={"mkv"}, min_bytes=20 * 1024 * 1024, max_bytes=0,
            exclude_disc=True,
        )
        == migration.FILTER_SMALL
    )
    assert (
        migration._filter(
            mkv, video_exts={"mkv"}, min_bytes=0, max_bytes=1024 * 1024,
            exclude_disc=True,
        )
        == migration.FILTER_LARGE
    )

    jpg = _entry("/影视/电影/x/poster.jpg", 1024, "p2")
    assert (
        migration._filter(
            jpg, video_exts={"mkv"}, min_bytes=0, max_bytes=0, exclude_disc=True
        )
        == migration.FILTER_EXT
    )

    # 隐藏备份文件（115 客户端残留）——不是正片，一律跳过
    backup = _entry("/影视/电影/x/.mp-backup-775a9488f5a690db5e11073e**..mp4", 2048, "p4")
    assert (
        migration._filter(
            backup, video_exts={"mkv", "mp4"}, min_bytes=0, max_bytes=0,
            exclude_disc=True,
        )
        == migration.FILTER_EXT
    )

    disc = _entry("/影视/电影/x/BDMV/STREAM/00800.m2ts", 1024, "p3")
    assert (
        migration._filter(
            disc, video_exts={"mkv", "m2ts"}, min_bytes=0, max_bytes=0,
            exclude_disc=True,
        )
        == migration.FILTER_EXT
    )
    assert (
        migration._filter(
            disc, video_exts={"mkv", "m2ts"}, min_bytes=0, max_bytes=0,
            exclude_disc=False,
        )
        is None
    )


def test_pickcode_from_task_url() -> None:
    item = {
        "taskId": "1",
        "res": "https://cdn.example/a.mkv?t=1&u=2&d=vip-1001300205-az4ojdw6rzs2qht9x-1-0&c=2",
    }
    assert migration._pickcode_from_task(item) == "az4ojdw6rzs2qht9x"
    assert migration._pickcode_from_task({"taskId": "1"}) == ""


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------


async def test_run_migration_end_to_end(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次跑通：成功 / 已存在 / 冲突 / 在途 / 过滤。"""
    await _seed_drivers(db_session)

    base = "/影视/电视剧/亚洲剧"
    show = f"{base}/Show (2025)/Season 1"

    files = [
        _entry(f"{show}/E01.mkv", 3_000_000_000, "new1"),
        _entry(f"{show}/E02.mkv", 2_000_000_000, "exists1"),
        _entry(f"{show}/E03.mkv", 2_500_000_000, "conflict1"),
        _entry(f"{show}/E04.mkv", 4_000_000_000, "inflight1"),
        _entry(f"{show}/E05.mkv", 10, "tiny1"),
        _entry(f"{show}/poster.jpg", 100, "jpg1"),
    ]
    p115 = _FakeP115(files)
    gyp = _FakeGuangya(
        {e.remote_id: (e.name, e.size) for e in files if e.name.endswith(".mkv")}
    )

    # 目标侧：E02 已存在（同大小）、E03 同名但大小不同
    gyp.seed_file("Show (2025)/Season 1", "E02.mkv", 2_000_000_000)
    gyp.seed_file("Show (2025)/Season 1", "E03.mkv", 1_000_000_000)
    # 上一轮遗留的在途任务 → 本轮跳过 E04
    gyp._legacy.append(
        {
            "taskId": "legacy-1",
            "res": "https://cdn/x?d=vip-1-inflight1-1-0",
        }
    )

    def _build(driver_type: str, *args: Any, **kwargs: Any) -> Any:
        return p115 if driver_type == "p115" else gyp

    monkeypatch.setattr(migration, "build_driver", _build)

    params = migration_jobs.MigrationParams(
        source_driver_id=101,
        source_cid="root-cid",
        source_path=base,
        target_driver_id=102,
        target_file_id="root",
        target_path="/影视",
        min_size_mb=1,
        concurrency=2,
        submit_interval_sec=0.0,
    )
    job = migration_jobs.MigrationJob(id="testjob", params=params)

    await migration.run_migration(job)

    assert job.success == 1, job.logs
    assert job.skipped_existing == 1
    assert job.skipped_inflight == 1
    assert job.conflicts == 1
    assert job.filtered_small == 1
    assert job.filtered_ext == 1
    assert job.failed == 0
    assert job.migrated_bytes == 3_000_000_000
    assert job.legacy_inflight == 1
    assert [p for p, _ in gyp.submitted] == ["new1"]
    # 目标目录层级与源目录镜像
    assert "Show (2025)" in gyp._dirs["root"]

    # 目标目录里确实出现了新文件（大小一致）
    show_id = gyp._dirs["root"]["Show (2025)"]
    season_id = gyp._dirs[show_id]["Season 1"]
    assert gyp._files[season_id]["E01.mkv"] == 3_000_000_000


async def test_run_migration_skips_fully_migrated_dir(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部已存在时不提交任何任务（可重复执行）。"""
    await _seed_drivers(db_session)

    base = "/影视/电影/动画电影"
    files = [_entry(f"{base}/A (2020)/A.mkv", 5_000_000_000, "a1")]
    p115 = _FakeP115(files)
    gyp = _FakeGuangya({"a1": ("A.mkv", 5_000_000_000)})
    gyp.seed_file("A (2020)", "A.mkv", 5_000_000_000)

    monkeypatch.setattr(
        migration, "build_driver", lambda *a, **k: p115 if a[0] == "p115" else gyp
    )

    params = migration_jobs.MigrationParams(
        source_driver_id=101,
        source_cid="root-cid",
        source_path=base,
        target_driver_id=102,
        target_file_id="root",
        target_path="/影视/电影/动画电影",
    )
    job = migration_jobs.MigrationJob(id="testjob2", params=params)

    await migration.run_migration(job)

    assert job.success == 0
    assert job.skipped_existing == 1
    assert gyp.submitted == []


async def test_submit_gate_spaces_out_submissions(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交必须错开：相邻两次提交的时间差 ≥ 设定间隔。

    **这是避开 115 下载频控的关键**——并发提交会让直链返回
    ``403 "115 pmt user 11-10"``，光鸭记为「云添加失败 code=8」。
    """
    await _seed_drivers(db_session)

    base = "/影视/电影/动画电影"
    files = [
        _entry(f"{base}/A (2020)/A{i}.mkv", 5_000_000_000, f"p{i}")
        for i in range(1, 4)
    ]
    p115 = _FakeP115(files)
    gyp = _FakeGuangya({e.remote_id: (e.name, e.size) for e in files})

    monkeypatch.setattr(
        migration, "build_driver", lambda *a, **k: p115 if a[0] == "p115" else gyp
    )

    interval = 0.2
    params = migration_jobs.MigrationParams(
        source_driver_id=101,
        source_cid="root-cid",
        source_path=base,
        target_driver_id=102,
        target_file_id="root",
        target_path="/影视/电影/动画电影",
        concurrency=3,  # 并发拉满，仍必须错开
        submit_interval_sec=interval,
    )
    job = migration_jobs.MigrationJob(id="testjob3", params=params)

    await migration.run_migration(job)

    assert len(gyp.submit_times) == 3, job.logs
    gaps = [
        b - a for a, b in zip(gyp.submit_times, gyp.submit_times[1:], strict=False)
    ]
    for gap in gaps:
        assert gap >= interval * 0.9, f"提交间隔不足：{gaps}"


async def test_submit_gate_zero_interval_does_not_wait() -> None:
    """间隔设为 0 时不等待（保留「不限速」的能力）。"""
    gate = migration._SubmitGate(0.0)
    assert await gate.wait() == 0.0
    assert await gate.wait() == 0.0


async def test_target_index_returns_copy() -> None:
    """``resolve`` 返回缓存的**副本**——调用方改了不会污染缓存。"""
    gyp = _FakeGuangya({"a": ("A.mkv", 100)})
    index = migration._TargetIndex(gyp, "root")

    _, files = await index.resolve("Show")
    files["injected"] = 999

    _, again = await index.resolve("Show")
    assert "injected" not in again


async def test_target_index_note_submitted_updates_cache() -> None:
    """提交后必须更新缓存——否则同目录的后续预检看不到刚提交的文件。"""
    gyp = _FakeGuangya({"a": ("A.mkv", 100)})
    index = migration._TargetIndex(gyp, "root")

    _, files = await index.resolve("Show")
    assert "A.mkv" not in files

    await index.note_submitted("Show", "A.mkv", 100)

    _, after = await index.resolve("Show")
    assert after.get("A.mkv") == 100
