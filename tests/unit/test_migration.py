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
from app.adapters.drivers.base import CloudFile, DirectLink, DriverError
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

    def __init__(
        self,
        info: dict[str, tuple[str, int]],
        landed_name: str | None = None,
    ) -> None:
        #: pickcode → (文件名, 大小)——提交任务后"落盘"用
        self._info = info
        #: 落盘名覆盖——光鸭直链的 URL 末段是 ``download``，云下载按它落名
        self._landed_name = landed_name
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
        #: 已提交但尚未被轮询取走的任务数峰值（用于断言「串行」）
        self.inflight = 0
        self.max_inflight = 0
        #: 改名的记录：``(旧名, 新名)``
        self.renamed: list[tuple[str, str]] = []
        #: 让 create_offline_task 抛错（额度耗尽场景）
        self.submit_error: str | None = None
        #: fileId → 文件名
        self._file_names: dict[str, str] = {}
        self._file_sizes: dict[str, int] = {}
        self._file_parents: dict[str, str] = {}

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

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        """带 fileId 的文件清单——改名要靠它定位。"""
        files = self._files.get(dir_id or "root", {})
        out: dict[str, tuple[int, str]] = {}
        for name, size in files.items():
            out[name] = (size, self._file_id_of(dir_id or "root", name))
        return out

    async def rename_file(self, file_id: str, new_name: str) -> None:
        """改名（fileId → 新名）。"""
        old_name = self._file_names.get(file_id)
        if old_name is None:
            raise AssertionError(f"未知 fileId：{file_id}")
        parent = self._file_parents[file_id]
        files = self._files.get(parent, {})
        size = files.pop(old_name, 0)
        files[new_name] = size
        self._file_names[file_id] = new_name
        self.renamed.append((old_name, new_name))

    def _file_id_of(self, dir_id: str, name: str) -> str:
        for fid, fname in self._file_names.items():
            if fname == name and self._file_parents.get(fid) == dir_id:
                return fid
        self._next_id += 1
        fid = f"file{self._next_id}"
        self._file_names[fid] = name
        self._file_sizes[fid] = self._files.get(dir_id, {}).get(name, 0)
        self._file_parents[fid] = dir_id
        return fid

    # -- 离线任务 --

    async def create_offline_task(self, url: str, parent_id: str = "") -> str:
        if self.submit_error:
            raise DriverError(self.submit_error, kind="unknown")
        key = url.rsplit("=", 1)[-1] if "=" in url else url.removeprefix("fake://")
        name, size = self._info[key]
        landed = self._landed_name or name
        target_dir = parent_id or "root"
        self._files.setdefault(target_dir, {})[landed] = size
        self._file_id_of(target_dir, landed)
        task_id = f"task-{key}"
        self._submitted.add(task_id)
        self.submitted.append((key, parent_id))
        self.submit_times.append(time.monotonic())
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        return task_id

    async def list_active_offline_tasks(self) -> list[dict[str, Any]]:
        # 上一轮遗留的任务永远在跑（本轮应跳过它对应的文件）
        items = list(self._legacy)
        for task_id in list(self._submitted):
            items.append({"taskId": task_id, "res": "https://cdn/x"})
            self._submitted.remove(task_id)  # 下一次轮询就"消失"
            self.inflight = max(0, self.inflight - 1)
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


# --------------------------------------------------------------------------
# 光鸭 → 光鸭（直链 → 云下载 → 改名）
# --------------------------------------------------------------------------


class _FakeGuangyaSource:
    """假光鸭源账号：直链 URL 末段是 ``download``（实测的真实形态）。"""

    name = "guangya"

    def __init__(self, files: list[CloudFile]) -> None:
        self._files = files
        #: (remote_id, user_agent)——光鸭源**不绑 UA**
        self.links: list[tuple[str, str]] = []

    async def iter_tree_by_cid(
        self, cid: str, parent_path: str = "", include_subdirs: bool = True
    ) -> AsyncIterator[CloudFile]:
        for entry in self._files:
            yield entry

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        self.links.append((remote_id, user_agent))
        return DirectLink(
            url=f"https://sh01-httpdown.guangyacdn.com/download/?fid={remote_id}",
            ua_required=False,
        )


async def _seed_guangya_drivers(db_session) -> None:
    """两个光鸭账号（源 + 目标）。

    凭据不同——测试里用它区分「源驱动」与「目标驱动」（两者类型都是 guangya）。
    """
    db_session.add(
        Driver(
            id=201,
            type="guangya",
            name="光鸭源",
            credential="src-cred",
            enabled=True,
            video_exts="mkv,mp4",
            created_at=int(time.time()),
        )
    )
    db_session.add(
        Driver(
            id=202,
            type="guangya",
            name="光鸭目标",
            credential="tgt-cred",
            enabled=True,
            created_at=int(time.time()),
        )
    )
    await db_session.commit()


def _guangya_build(src: Any, gyp: Any):
    """造一个 build_driver 替身：按凭据区分源 / 目标。"""

    def _build(driver_type: str, credential: str | None = None, *a: Any, **k: Any) -> Any:
        return src if credential == "src-cred" else gyp

    return _build


async def test_guangya_source_end_to_end(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """光鸭源端到端：落盘名 ``download`` → 改名回原名 → 校验通过。"""
    await _seed_guangya_drivers(db_session)

    files = [
        _entry("/影视/电影/A (2020)/A.mkv", 1_000_000, "f1"),
        _entry("/影视/电影/A (2020)/B.mkv", 2_000_000, "f2"),
    ]
    src = _FakeGuangyaSource(files)
    gyp = _FakeGuangya(
        {"f1": ("A.mkv", 1_000_000), "f2": ("B.mkv", 2_000_000)},
        landed_name="download",
    )

    monkeypatch.setattr(migration, "build_driver", _guangya_build(src, gyp))

    params = migration_jobs.MigrationParams(
        source_driver_id=201,
        source_cid="1962124234139361322",
        source_path="/影视/电影",
        target_driver_id=202,
        target_file_id="root",
        target_path="/影视",
        concurrency=5,  # 光鸭源必须强制收敛到 1
    )
    job = migration_jobs.MigrationJob(id="gypjob", params=params)

    await migration.run_migration(job)

    assert job.success == 2, job.logs
    assert job.failed == 0
    # 直链不绑 UA
    assert all(ua == "" for _, ua in src.links), src.links
    # 落盘名是 download → 被改名回原名
    assert sorted(old for old, _ in gyp.renamed) == ["download", "download"]
    assert sorted(new for _, new in gyp.renamed) == ["A.mkv", "B.mkv"]
    # 目标目录里是原名 + 正确大小
    show_id = gyp._dirs["root"]["A (2020)"]
    assert gyp._files[show_id]["A.mkv"] == 1_000_000
    assert gyp._files[show_id]["B.mkv"] == 2_000_000
    # 恒串行：任何时刻只有一个任务在途
    assert gyp.max_inflight == 1, f"并发没收敛：{gyp.max_inflight}"


async def test_guangya_source_adopts_leftover(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """中断恢复：目标目录里已落盘但没改名的 ``download`` → 认领改名，不重下。"""
    await _seed_guangya_drivers(db_session)

    files = [_entry("/影视/电影/A (2020)/A.mkv", 1_000_000, "f1")]
    src = _FakeGuangyaSource(files)
    gyp = _FakeGuangya({"f1": ("A.mkv", 1_000_000)}, landed_name="download")
    # 上一轮已落盘（名字还是 download）
    gyp.seed_file("A (2020)", "download", 1_000_000)

    monkeypatch.setattr(migration, "build_driver", _guangya_build(src, gyp))

    params = migration_jobs.MigrationParams(
        source_driver_id=201,
        source_cid="1962124234139361322",
        source_path="/影视/电影",
        target_driver_id=202,
        target_file_id="root",
    )
    job = migration_jobs.MigrationJob(id="gypjob2", params=params)

    await migration.run_migration(job)

    assert job.skipped_existing == 1, job.logs
    assert gyp.submitted == [], "不该重复提交（云添加额度稀缺）"
    show_id = gyp._dirs["root"]["A (2020)"]
    assert gyp._files[show_id]["A.mkv"] == 1_000_000


async def test_quota_exhausted_pauses_job(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    """云添加额度耗尽 → 作业**暂停**（不硬撞、不重试），次日重跑即可续传。"""
    await _seed_guangya_drivers(db_session)

    files = [_entry("/影视/电影/A (2020)/A.mkv", 1_000_000, "f1")]
    src = _FakeGuangyaSource(files)
    gyp = _FakeGuangya({"f1": ("A.mkv", 1_000_000)}, landed_name="download")
    gyp.submit_error = "光鸭创建离线任务失败：云添加次数超限，下个自然日刷新"

    monkeypatch.setattr(migration, "build_driver", _guangya_build(src, gyp))

    params = migration_jobs.MigrationParams(
        source_driver_id=201,
        source_cid="1962124234139361322",
        source_path="/影视/电影",
        target_driver_id=202,
        target_file_id="root",
    )
    job = migration_jobs.MigrationJob(id="gypjob3", params=params)

    await migration.run_migration(job)

    assert job.state == migration_jobs.STATE_PAUSED, job.logs
    assert job.error and "次数超限" in job.error
    assert job.failed == 0, "额度耗尽不是文件失败"


def test_landed_name_from_url() -> None:
    """落盘名 = URL 末段（光鸭 CDN 直链末段是 ``download``）。"""
    assert (
        migration._landed_name_from_url(
            "https://sh01-httpdown.guangyacdn.com/download/?fid=abc"
        )
        == "download"
    )
    assert (
        migration._landed_name_from_url("https://cdn.115.com/影视/A.mkv?uid=1")
        == "A.mkv"
    )
    assert migration._landed_name_from_url("") == ""
