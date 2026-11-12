"""光鸭 → 光鸭 转存引擎的单元测试。

覆盖点（全部对应一条真实契约）：

- **逐文件判冲突**：同名同大小 → 跳过；同名不同大小 → 记冲突，
  **不调保存**（否则服务端会自动改名 ``xxx(1)`` 产出垃圾副本）
- **按目录分批**：提交单元 = 同一源目录下的文件；单批上限 500
- **清理源是可选开关**，且只删**已校验成功**的文件
- 建分享撞限流（``201``）→ 退避重试；``112`` 参数错误 → **不重试**
- 分享内容缺文件 → 记失败（不静默少转）

用假驱动（不联网、不碰真实光鸭账号）。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.adapters.drivers.base import CloudFile, DriverError
from app.adapters.drivers.guangya import ShareInfo
from app.services import cloud_relay
from app.services.relay_jobs import RelayJob, RelayParams


def _file(name: str, size: int = 100, rel: str = "") -> CloudFile:
    return CloudFile(
        remote_id=f"id-{name}-{rel}",
        path=f"/{rel}/{name}" if rel else f"/{name}",
        name=name,
        size=size,
        is_dir=False,
        mtime=1700000000,
        relative_dir=rel,
    )


class FakeRelayDriver:
    """假的光鸭驱动（源 / 目标各一个）。"""

    def __init__(self) -> None:
        #: 源驱动：产出的文件
        self.files: list[CloudFile] = []
        #: 目标驱动：已存在的文件 ``{文件名: (大小, fileId)}``
        self.existing: dict[str, tuple[int, str]] = {}
        self.created_dirs: list[str] = []
        self.shares: list[list[str]] = []
        self.restores: list[tuple[list[str], str]] = []
        self.deleted_shares: list[str] = []
        self.deleted_files: list[str] = []
        #: 建分享时抛的错（第几次抛）
        self.share_error: DriverError | None = None
        self.share_error_times = 0
        #: 分享内容里缺哪些 id（模拟服务端没收录）
        self.share_missing: set[str] = set()
        #: 文件目录 ``{fileId: 文件}``——目标驱动靠它知道落盘的文件名
        self.catalog: dict[str, CloudFile] = {}

    async def iter_tree_by_cid(self, cid: str, parent_path: str = "", include_subdirs: bool = True):
        for item in self.files:
            yield item

    async def ensure_dir(self, rel_path: str, root_id: str = "") -> str:
        self.created_dirs.append(rel_path)
        return f"dir::{rel_path}"

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        return dict(self.existing)

    async def create_share(self, file_ids: list[str], title: str) -> ShareInfo:
        if self.share_error is not None and self.share_error_times > 0:
            self.share_error_times -= 1
            raise self.share_error
        self.shares.append(list(file_ids))
        return ShareInfo(
            share_id=f"share-{len(self.shares)}",
            code="",
            share_url=f"https://www.guangyapan.com/s/share-{len(self.shares)}",
        )

    async def share_access_token(self, share_id: str, code: str = "") -> str:
        return "tok"

    async def share_files_list(self, token: str) -> list[dict[str, Any]]:
        ids = self.shares[-1] if self.shares else []
        return [
            {"fileId": i, "fileName": "x", "fileSize": 1}
            for i in ids
            if i not in self.share_missing
        ]

    async def restore_share(self, token: str, file_ids: list[str], parent_id: str = "") -> str:
        self.restores.append((list(file_ids), parent_id))
        # 模拟服务端把文件落到目标目录（同名不同名都不改名）
        for fid in file_ids:
            item = self.catalog.get(fid)
            if item is not None:
                self.existing[item.name] = (item.size, f"new-{fid}")
        return "task-1"

    async def delete_share(self, share_id: str) -> None:
        self.deleted_shares.append(share_id)

    async def delete_file(self, file_id: str) -> None:
        self.deleted_files.append(file_id)


@pytest.fixture(autouse=True)
def fast_verify(monkeypatch: pytest.MonkeyPatch):
    """校验落盘的等待压到毫秒级——测试不该真等 5 分钟。"""
    monkeypatch.setattr(cloud_relay, "VERIFY_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(cloud_relay, "VERIFY_INTERVAL_SEC", 0.01)


@pytest.fixture
async def driver_rows(db_session):
    """两个光鸭存储源（源 id=1 / 目标 id=2）。"""
    from app.db.models import Driver

    for did, name in ((1, "源盘"), (2, "目标盘")):
        db_session.add(
            Driver(
                id=did,
                type="guangya",
                name=name,
                credential=None,
                device_type=None,
                enabled=True,
                media_cid=None,
                media_path=None,
                output_dir=None,
                video_exts="",
                image_exts="",
                data_exts="",
                min_size_mb=0,
                include_subdirs=True,
                created_at=int(time.time()),
                updated_at=int(time.time()),
            )
        )
    await db_session.commit()


def _patch_drivers(monkeypatch: pytest.MonkeyPatch, src: FakeRelayDriver, tgt: FakeRelayDriver):
    queue = [src, tgt]
    # 目标驱动靠 catalog 知道「保存后落盘的文件叫什么名」
    tgt.catalog = {f.remote_id: f for f in src.files}

    def _build(*args: Any, **kwargs: Any) -> FakeRelayDriver:
        return queue.pop(0)

    monkeypatch.setattr(cloud_relay, "build_driver", _build)


def _job(**kwargs: Any) -> RelayJob:
    params = RelayParams(
        source_driver_id=kwargs.pop("source_driver_id", 1),
        source_file_id=kwargs.pop("source_file_id", "root"),
        target_driver_id=kwargs.pop("target_driver_id", 2),
        target_file_id=kwargs.pop("target_file_id", "tgt"),
        **kwargs,
    )
    return RelayJob(id="job-1", params=params)


class TestTransfer:
    async def test_transfers_files(self, driver_rows, monkeypatch) -> None:
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100), _file("b.mkv", 200)]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        job = _job()
        await cloud_relay.run_relay(job)

        assert job.success == 2
        assert job.failed == 0
        assert len(tgt.restores) == 1
        assert tgt.restores[0][1] == "dir::"
        # 分享用完即删
        assert tgt.deleted_shares == []
        assert src.deleted_shares == ["share-1"]

    async def test_creates_target_dirs_per_relative_path(
        self, driver_rows, monkeypatch
    ) -> None:
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100, "Movie A"), _file("b.mkv", 100, "Movie A/Extras")]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        job = _job()
        await cloud_relay.run_relay(job)

        assert sorted(tgt.created_dirs) == ["Movie A", "Movie A/Extras"]
        assert job.success == 2

    async def test_skips_same_name_same_size(self, driver_rows, monkeypatch) -> None:
        """同名同大小 → 跳过，且**不调保存**。"""
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        tgt = FakeRelayDriver()
        tgt.existing = {"a.mkv": (100, "old-id")}
        _patch_drivers(monkeypatch, src, tgt)

        job = _job()
        await cloud_relay.run_relay(job)

        assert job.skipped_existing == 1
        assert job.success == 0
        assert tgt.restores == [], "跳过的文件不该触发保存（会产出 (1) 副本）"

    async def test_records_conflict_on_size_mismatch(
        self, driver_rows, monkeypatch
    ) -> None:
        """同名不同大小 → 记冲突，不覆盖、不改名、不保存。"""
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        tgt = FakeRelayDriver()
        tgt.existing = {"a.mkv": (999, "old-id")}
        _patch_drivers(monkeypatch, src, tgt)

        job = _job()
        await cloud_relay.run_relay(job)

        assert job.conflicts == 1
        assert job.success == 0
        assert tgt.restores == []
        assert job.conflict_items[0]["name"] == "a.mkv"


class TestBatching:
    async def test_splits_at_500(self, driver_rows, monkeypatch) -> None:
        """单批上限 500——超了要拆批（服务端会拒超限的批次）。"""
        src = FakeRelayDriver()
        src.files = [_file(f"f{i}.mkv", 10) for i in range(600)]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        job = _job()
        await cloud_relay.run_relay(job)

        assert len(tgt.restores) == 2
        assert len(tgt.restores[0][0]) == 500
        assert len(tgt.restores[1][0]) == 100
        assert job.success == 600


class TestDeleteSource:
    async def test_disabled_by_default(self, driver_rows, monkeypatch) -> None:
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        job = _job(delete_source=False)
        await cloud_relay.run_relay(job)

        assert src.deleted_files == []

    async def test_deletes_only_verified_files(self, driver_rows, monkeypatch) -> None:
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        job = _job(delete_source=True)
        await cloud_relay.run_relay(job)

        assert src.deleted_files == ["id-a.mkv-"]

    async def test_skips_delete_when_save_missing(self, driver_rows, monkeypatch) -> None:
        """服务端没把文件落下来 → **不能删源**（否则数据没了）。"""
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)
        # 保存成功但目标目录里查不到（同名的被改名成 (1) 的情形）
        orig_restore = tgt.restore_share

        async def _restore(token: str, file_ids: list[str], parent_id: str = "") -> str:
            await orig_restore(token, file_ids, parent_id)
            tgt.existing.clear()  # 落盘「没有」同名文件
            return "task-1"

        tgt.restore_share = _restore  # type: ignore[method-assign]

        job = _job(delete_source=True)
        await cloud_relay.run_relay(job)

        assert job.failed == 1
        assert src.deleted_files == []


class TestShareRetry:
    async def test_retries_on_rate_limit(self, driver_rows, monkeypatch) -> None:
        """``201`` 分享已失效 → 退避重试（等一会儿建就好）。"""
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        src.share_error = DriverError("分享已失效", kind="rate_limit")
        src.share_error_times = 1
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        monkeypatch.setattr(cloud_relay, "_sleep", _no_sleep)

        job = _job(submit_interval_sec=1)
        await cloud_relay.run_relay(job)

        assert job.success == 1
        assert len(src.shares) == 1

    async def test_does_not_retry_on_param_error(self, driver_rows, monkeypatch) -> None:
        """``112`` 参数错误是调用方拼错了——重试没有意义。"""
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        src.share_error = DriverError("参数错误", kind="config")
        src.share_error_times = 5
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        monkeypatch.setattr(cloud_relay, "_sleep", _no_sleep)

        job = _job(submit_interval_sec=1)
        with pytest.raises(DriverError):
            await cloud_relay.run_relay(job)
        assert src.shares == []


class TestShareContent:
    async def test_missing_in_share_records_failure(self, driver_rows, monkeypatch) -> None:
        """分享内容缺文件 → 记失败（不静默少转）。"""
        src = FakeRelayDriver()
        src.files = [_file("a.mkv", 100)]
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        original = src.share_files_list

        async def _list(token: str) -> list[dict[str, Any]]:
            src.share_missing = {f.remote_id for f in src.files}
            return await original(token)

        src.share_files_list = _list  # type: ignore[method-assign]

        job = _job()
        await cloud_relay.run_relay(job)

        assert job.failed == 1
        assert tgt.restores == [], "内容不全时不该提交保存"


class TestValidation:
    async def test_rejects_same_account(self, driver_rows, monkeypatch) -> None:
        src = FakeRelayDriver()
        tgt = FakeRelayDriver()
        _patch_drivers(monkeypatch, src, tgt)

        job = _job(source_driver_id=1, target_driver_id=1)
        with pytest.raises(cloud_relay.RelayError, match="同一个光鸭账号"):
            await cloud_relay.run_relay(job)


async def _no_sleep(_delay: float, *_args: Any, **_kwargs: Any) -> None:
    """把退避 sleep 压成 0（测试不该真等）。"""
    return None
