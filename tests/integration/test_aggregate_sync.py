"""归拢引擎（副盘 → 主盘）的集成测试。

用**假驱动**跑完整链路（不打云端 API），验证四条关键行为：

1. 归拢成功后**自动清理副盘副本**（主盘确认落盘同名同大小之后才删）
2. 主盘已有同名同大小 → 跳过传输，直接清副盘副本
3. 主盘已有更大版本 → 保留大的，跳过传输
4. 云添加额度耗尽 → 转 ``paused``，**不硬撞**（不提交）

外加：单条失败不炸整轮、失败原因落到台账里。
"""

from __future__ import annotations

import pytest
from app.adapters.drivers.base import DirectLink, DriverError
from app.db.models import SecondaryStaging
from app.db.session import session_scope
from app.services import (
    aggregate_groups,
    aggregate_staging,
    aggregate_sync,
    driver_service,
)
from sqlalchemy import select


class _FakeMaster:
    """假主盘驱动：模拟云下载落盘（名为 ``download``）→ 改名 → 校验。"""

    def __init__(self, landed_size: int = 0) -> None:
        self.files: dict[str, tuple[int, str]] = {}
        self.dirs: dict[str, str] = {"": "root"}
        self.landed_size = landed_size
        self.submitted: list[str] = []
        self.renamed: list[tuple[str, str]] = []

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        dir_id = f"dir-{name}"
        self.dirs[name] = dir_id
        return dir_id, True

    async def list_files(self, dir_id: str) -> dict[str, int]:
        return {name: size for name, (size, _fid) in self.files.items()}

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        return dict(self.files)

    async def rename_file(self, file_id: str, new_name: str) -> None:
        self.renamed.append((file_id, new_name))
        for name, (size, fid) in list(self.files.items()):
            if fid == file_id:
                del self.files[name]
                self.files[new_name] = (size, fid)
                return

    async def create_offline_task(self, url: str, parent_id: str = "") -> str:
        self.submitted.append(url)
        # 模拟云下载落盘：文件名取自 URL 末段（光鸭直链末段 = download）
        self.files["download"] = (self.landed_size, "landed-1")
        return "task-1"

    async def list_active_offline_tasks(self) -> list[dict[str, object]]:
        return []

    async def list_failed_offline_tasks(self) -> list[dict[str, object]]:
        return []

    async def delete_file(self, file_id: str) -> None:
        pass


class _FakeSource:
    """假副盘驱动：只提供直链与删除。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.links: list[str] = []

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        self.links.append(remote_id)
        return DirectLink(
            url="https://pool-httpdown.guangyacdn.com/download/?fid=x",
            ua_required=False,
        )

    async def delete_file(self, file_id: str) -> None:
        self.deleted.append(file_id)


@pytest.fixture
def fast_watcher(monkeypatch: pytest.MonkeyPatch):
    """把任务轮询间隔压到 10ms（默认 5s 会让测试跑成慢动作）。"""

    class _FastWatcher(aggregate_sync.TaskWatcher):
        def __init__(self, driver, *, log=None, poll_interval=None) -> None:
            super().__init__(driver, log=log, poll_interval=0.01)

    monkeypatch.setattr(aggregate_sync, "TaskWatcher", _FastWatcher)


@pytest.fixture
def fake_drivers(monkeypatch: pytest.MonkeyPatch):
    """按凭据区分主盘 / 副盘返回假驱动。"""
    master = _FakeMaster()
    source = _FakeSource()

    def _build(driver_type, credential=None, device_type=None, **kwargs):
        if credential == "master":
            return master
        return source

    monkeypatch.setattr(aggregate_sync, "build_driver", _build)
    return master, source


async def _setup(db, *, size: int = 100) -> tuple[int, int, int]:
    """建组 + 主盘（带媒体库目录）+ 副盘 + 一条在途台账。"""
    master = await driver_service.add_driver(driver_type="guangya", name="主号")
    secondary = await driver_service.add_driver(driver_type="guangya", name="副号")
    group = await aggregate_groups.create_group("G")
    await aggregate_groups.assign_driver_role(
        master.id, group_id=group.id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        secondary.id, group_id=group.id, role="secondary"
    )
    # 给两个盘写上可区分的凭据（假驱动按凭据分派）
    from app.db.models import Driver

    async with session_scope() as db_session:
        rows = (
            (
                await db_session.execute(
                    select(Driver).where(Driver.id.in_([master.id, secondary.id]))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.credential = "master" if row.id == master.id else "secondary"
            if row.id == master.id:
                row.media_cid = "root"
        await db_session.commit()

    async with session_scope() as db_session:
        db_session.add(
            SecondaryStaging(
                group_id=group.id,
                source_driver_id=secondary.id,
                target_driver_id=master.id,
                remote_id="f1",
                file_name="a.mkv",
                size_bytes=size,
                state="pending",
            )
        )
        await db_session.commit()
    return group.id, master.id, secondary.id


async def test_sync_moves_file_and_cleans_secondary(
    db, fast_watcher, fake_drivers
) -> None:
    """归拢成功 → 主盘落盘 → **副盘副本自动清理**。"""
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.synced == 1
    assert result.cleaned == 1
    assert result.failed == 0

    # 云下载被提交过（直链末段 = download → 落盘名 download）
    assert len(master.submitted) == 1
    # 改名回原名
    assert ("landed-1", "a.mkv") in master.renamed
    # 副盘副本被删
    assert source.deleted == ["f1"]

    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "done"
    assert rows[0].cleaned_at is not None


async def test_sync_skips_when_master_already_has_same_file(
    db, fast_watcher, fake_drivers
) -> None:
    """主盘已有同名同大小 → 跳过传输，直接清副盘副本（不白扣额度）。"""
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.files["a.mkv"] = (100, "existing")

    result = await aggregate_sync.sync_group_now(group_id)
    assert master.submitted == []  # 没提交云下载
    assert source.deleted == ["f1"]  # 副盘冗余副本被清掉
    assert result.skipped == 1
    assert result.failed == 0


async def test_sync_keeps_larger_master_version(
    db, fast_watcher, fake_drivers
) -> None:
    """主盘已有**更大**版本 → 保留大的，跳过传输。"""
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.files["a.mkv"] = (200, "existing")

    result = await aggregate_sync.sync_group_now(group_id)
    assert master.submitted == []
    assert source.deleted == ["f1"]
    assert result.skipped == 1


async def test_sync_pauses_group_when_hitting_quota_wall(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """撞上额度墙 → **整组**待归拢行转 ``paused``，并停手（不逐个撞）。

    撞墙 = 当天的门关了；继续提交只是白费服务端请求（额度本来就没了）。
    """
    master, source = fake_drivers
    group_id, master_id, _secondary_id = await _setup(db, size=100)

    # 再加一条在途任务（两个文件待归拢）
    async with session_scope() as db_session:
        db_session.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=_secondary_id,
                target_driver_id=master_id,
                remote_id="f2",
                file_name="b.mkv",
                size_bytes=100,
                state="pending",
            )
        )
        await db_session.commit()

    async def _wall(url: str, parent_id: str = "") -> str:
        raise DriverError(
            "光鸭创建离线任务失败：云添加次数超限，下个自然日刷新", kind="unknown"
        )

    monkeypatch.setattr(master, "create_offline_task", _wall)

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.paused == 2  # 两个文件都转暂停
    assert result.failed == 0
    assert source.deleted == []

    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert {r.state for r in rows} == {"paused"}
    assert all("次数超限" in (r.last_error or "") for r in rows)


async def test_paused_rows_resume_after_quota_resets(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """暂停的行**次日自动续传**——额度恢复后作业把 paused 行捡起来继续。"""
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)

    async def _wall(url: str, parent_id: str = "") -> str:
        raise DriverError(
            "光鸭创建离线任务失败：云添加次数超限，下个自然日刷新", kind="unknown"
        )

    monkeypatch.setattr(master, "create_offline_task", _wall)
    await aggregate_sync.sync_group_now(group_id)
    assert (await aggregate_staging.list_staging(group_id=group_id))[0].state == "paused"

    # 次日：额度恢复 → 恢复正常提交
    master.landed_size = 100
    monkeypatch.setattr(
        master,
        "create_offline_task",
        lambda url, parent_id="": master.__class__.create_offline_task(
            master, url, parent_id
        ),
    )
    result = await aggregate_sync.sync_group_now(group_id)
    assert result.synced == 1
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "done"


async def test_generic_failure_is_failed_and_not_auto_retried(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """泛化失败（「云添加失败」）→ 记 ``failed``，且**不自动重试**。

    泛化失败认不出是不是额度问题，而**失败也扣额度** → 自动重试就是白扣。
    只由人工点「重试」才再试。
    """
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)

    async def _boom(url: str, parent_id: str = "") -> str:
        raise DriverError("光鸭创建离线任务失败：云添加失败", kind="unknown")

    monkeypatch.setattr(master, "create_offline_task", _boom)

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.failed == 1
    assert result.paused == 0

    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "failed"

    # 下一轮**不再自动重试**（没有新的提交）
    await aggregate_sync.sync_group_now(group_id)
    assert len(master.submitted) == 0

    # 人工点「重试」→ 回到待归拢，下一轮才会再试
    await aggregate_staging.retry_staging(rows[0].id)
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "pending"


async def test_rename_does_not_touch_pre_existing_download_file(
    db, fast_watcher, fake_drivers
) -> None:
    """目标目录里**原本就有** ``download`` 文件时，不得把它改名。

    用户自己的文件被改名 = 不可逆的数据损坏。落地文件名（``download``）
    与用户文件同名时，只认服务端自动改名形态之外的情况 → 认不出就不改。
    """
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    # 用户原有的 download 文件（字节数恰好一致——最危险的情形）
    master.files["download"] = (100, "user-file")

    result = await aggregate_sync.sync_group_now(group_id)
    # 落地文件被服务端改成了 download(1)（因同名）——但我们**不认领**
    # 「用户原有文件」；落地的那份留着没改名 → 校验不过 → 记失败
    assert ("user-file", "a.mkv") not in master.renamed
    assert result.failed == 1
