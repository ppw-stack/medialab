"""归拢引擎（副盘 → 主盘）的集成测试。

用**假驱动**跑完整链路（不打云端 API），验证四条关键行为：

1. 归拢成功后**自动清理副盘副本**（主盘确认落盘同名同大小之后才删）
2. 主盘已有同名同大小 → 跳过传输，直接清副盘副本
3. 主盘已有更大版本 → 保留大的，跳过传输
4. 云添加额度耗尽 → 转 ``paused``，**不硬撞**（不提交）

外加：单条失败不炸整轮、失败原因落到台账里。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from app.adapters.drivers.base import DirectLink, DriverError
from app.db.models import SecondaryStaging
from app.db.session import session_scope
from app.services import (
    aggregate_groups,
    aggregate_jobs,
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
        #: 落地文件的自增序号（fileId / taskId 都用它 → **保证唯一**）
        self._seq = 0
        # -- 分享/转存 的记录与故障注入 --
        self.shares_created: list[tuple[list[str], str]] = []
        self.shares_deleted: list[list[str]] = []
        self.access_tokens: list[tuple[str, str]] = []
        self.restored: list[tuple[list[str], str]] = []
        #: 副盘文件 id → 文件名（转存时用来还原原名）
        self.source_names: dict[str, str] = {}
        self.share_token_error: str | None = None
        self.restore_error: str | None = None

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        dir_id = f"dir-{name}"
        self.dirs[name] = dir_id
        return dir_id, True

    # -- 分享 / 转存（小文件通道）--

    async def create_share(self, file_ids: list[str], title: str) -> Any:
        """建分享（假实现）。"""
        self.shares_created.append((list(file_ids), title))
        return SimpleNamespace(
            share_id="share-1",
            url_id="share-1_key",
            code="abcd",
            title=title,
        )

    async def share_access_token(self, url_id: str, code: str) -> str:
        self.access_tokens.append((url_id, code))
        if self.share_token_error:
            raise DriverError(self.share_token_error, kind="unknown")
        return "token-1"

    async def restore_share(
        self, access_token: str, file_ids: list[str], parent_id: str
    ) -> str:
        """转存（假实现）：把文件**直接以原名**放进目标目录。"""
        self.restored.append((list(file_ids), parent_id))
        if self.restore_error:
            raise DriverError(self.restore_error, kind="unknown")
        for fid in file_ids:
            name = self.source_names.get(fid, f"file-{fid}")
            self.files[name] = (self.landed_size, f"restored-{fid}")
        return "restore-task-1"

    async def delete_share(self, share_ids: list[str]) -> None:
        self.shares_deleted.append(list(share_ids))

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
        # 模拟云下载落盘：文件名取自 URL 末段（光鸭直链末段 = download）；
        # 目录里已有同名文件时，服务端自动改名 download(1)、download(2)…
        name = "download"
        n = 1
        while name in self.files:
            name = f"download({n})"
            n += 1
        # ⚠️ **fileId 每次都不同**——真实光鸭每次落地都是新 fileId。
        #   早期这里用 ``landed-{n}``，而 n 只在撞名时才 +1 → 多个文件
        #   fileId 相同 → 改名时改到了**别的文件**上（测试假象）。
        self._seq += 1
        self.files[name] = (self.landed_size, f"landed-{self._seq}")
        return f"task-{self._seq}"

    async def list_active_offline_tasks(self) -> list[dict[str, object]]:
        return []

    async def list_failed_offline_tasks(self) -> list[dict[str, object]]:
        return []

    async def delete_file(self, file_id: str) -> None:
        pass


class _FakeSource:
    """假副盘驱动：直链 / 删除 / 分享（源账号侧）。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.links: list[str] = []
        self.shares_created: list[tuple[list[str], str]] = []
        self.shares_deleted: list[list[str]] = []
        self.access_tokens: list[tuple[str, str]] = []

    async def get_direct_link(self, remote_id: str, user_agent: str) -> DirectLink:
        self.links.append(remote_id)
        return DirectLink(
            url="https://pool-httpdown.guangyacdn.com/download/?fid=x",
            ua_required=False,
        )

    async def delete_file(self, file_id: str) -> None:
        self.deleted.append(file_id)

    # -- 分享（源账号侧）--

    async def create_share(self, file_ids: list[str], title: str) -> Any:
        self.shares_created.append((list(file_ids), title))
        return SimpleNamespace(
            share_id="share-1",
            url_id="share-1_key",
            code="abcd",
            title=title,
        )

    async def share_access_token(self, url_id: str, code: str) -> str:
        self.access_tokens.append((url_id, code))
        return "token-1"

    async def delete_share(self, share_ids: list[str]) -> None:
        self.shares_deleted.append(list(share_ids))


@pytest.fixture(autouse=True)
def _default_offline_channel(monkeypatch: pytest.MonkeyPatch):
    """默认**关掉**「分享转存」通道——只有专门测它的用例才打开。

    不关的话，多数用例会走分享通道，而假主盘的转存落地名与校验期望名不
    匹配 → 每个文件轮询满 600 秒 → 整个测试文件跑到天荒地老。
    """
    monkeypatch.setattr(aggregate_sync, "_share_threshold_bytes", lambda: 0)


@pytest.fixture(autouse=True)
def _no_real_driver(monkeypatch: pytest.MonkeyPatch):
    """把「登记在途」用的驱动换成假的——**不许真连光鸭**。

    登记在途会真去列副盘的目录（生产 16 个请求/盘）；测试里连真网盘会
    又慢又不稳（还会撞真实频率限制）。
    """

    class _Empty:
        async def list_dir_by_cid(self, cid, parent_path=""):
            return []

        async def iter_tree_by_cid(self, cid, parent_path="", include_subdirs=True):
            return
            yield  # pragma: no cover - 空迭代器

    monkeypatch.setattr(aggregate_staging, "build_driver", lambda *a, **k: _Empty())


@pytest.fixture(autouse=True)
def _clear_group_locks():
    """每个用例前清掉**组级锁**。

    锁是模块级字典、按 group id 索引——而测试里每个用例的组 id 都是 1
    （每个用例一份新库）。上一个用例若异常退出（没释放锁），下一个用例
    就会**永久阻塞**在 ``async with lock`` 上。
    """
    aggregate_sync._GROUP_LOCKS.clear()
    yield
    aggregate_sync._GROUP_LOCKS.clear()


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
        await db_session.commit()

    # 归拢落点是**组自己的配置**（不是主盘的媒体库目录）
    await aggregate_groups.set_mapping(
        group.id,
        target_dir_id="root",
        target_dir_path="影视",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=secondary.id, root_id="buf", root_path="影视"
            )
        ],
    )

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


async def test_missing_group_raises_404(db) -> None:
    """手动归拢一个不存在的组 → 404（不是静默成功 + 全 0 统计）。"""
    from app.services.errors import ServiceError

    with pytest.raises(ServiceError) as exc:
        await aggregate_sync.sync_group_now(999999)
    assert exc.value.status == 404


async def test_group_lock_serializes_concurrent_runs(
    db, fast_watcher, fake_drivers
) -> None:
    """组级锁：手动触发与 cron 作业重叠时**互斥**（不重复提交）。"""
    import asyncio

    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    # 别人持锁 → sync_group_now 必须等待（拿到锁才跑）
    lock = await aggregate_sync._group_lock(group_id)
    await lock.acquire()
    task = asyncio.create_task(aggregate_sync.sync_group_now(group_id))
    await asyncio.sleep(0.05)
    assert not task.done(), "锁被持有时应等待，而不是并行跑"

    lock.release()
    result = await asyncio.wait_for(task, timeout=10)
    assert result.groups == 1
    assert len(master.submitted) == 1


async def test_active_task_query_failure_skips_syncing_rows(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """在途任务**查询失败** → 本轮跳过 syncing 行（保守不碰）。

    不能把「查询失败」当「任务已消失」——那会把它重置为待归拢并重新提交，
    同一文件双任务（白扣额度 + 主盘留垃圾）。128 容器 DNS 偶发故障是已知
    触发源。
    """
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)

    async def _boom() -> list[dict[str, object]]:
        raise DriverError(" Temporary failure in name resolution", kind="upstream")

    # 行已在途（上一轮提交了云下载、任务还在跑）
    async with session_scope() as db_session:
        row = (
            await db_session.execute(
                select(SecondaryStaging).where(
                    SecondaryStaging.group_id == group_id
                )
            )
        ).scalar_one()
        row.state = "syncing"
        row.cloud_task_id = "task-9"
        await db_session.commit()

    monkeypatch.setattr(master, "list_active_offline_tasks", _boom)

    result = await aggregate_sync.sync_group_now(group_id)
    assert master.submitted == [], "查询失败时绝不能重新提交"
    assert result.failed == 0 and result.skipped == 0 and result.synced == 0
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "syncing", "应原样保留 syncing，等下轮查询恢复"


async def test_stuck_task_times_out_and_marks_failed(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """光鸭卡单（任务永远不离开活跃列表）→ 超时记 failed，run 不被拖死。

    无超时的话 run 永不结束 → APScheduler max_instances=1 → 后续触发全部
    跳过 → 其它组的归拢也饿死。
    """
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    monkeypatch.setattr(aggregate_sync, "TASK_MAX_WAIT_SEC", 0.05)

    # 任务永远在活跃列表（模拟卡单）
    async def _sticky(url: str, parent_id: str = "") -> str:
        master.files["download"] = (100, "landed-1")
        return "task-stuck"

    async def _active() -> list[dict[str, object]]:
        return [{"taskId": "task-stuck"}]

    monkeypatch.setattr(master, "create_offline_task", _sticky)
    monkeypatch.setattr(master, "list_active_offline_tasks", _active)

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.failed == 1
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "failed"
    assert "小时仍未结束" in (rows[0].last_error or "")
    assert source.deleted == [], "超时未确认落盘，绝不清理副盘副本"


async def test_manual_sync_runs_in_background(db, fast_watcher, fake_drivers) -> None:
    """手动归拢走后台任务：立即返回 job_id，轮询到结束，结果齐整。"""
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    group = await aggregate_groups.get_group(group_id)
    job = await aggregate_jobs.start_sync_job(group_id, group.name)
    assert job.state == "running"

    for _ in range(100):
        await asyncio.sleep(0.05)
        if job.done:
            break
    assert job.state == "success", job.logs
    assert job.result.synced == 1 and job.result.cleaned == 1
    assert any("已归拢" in line for line in job.logs)

    # 任务表可查
    found = await aggregate_jobs.get_job(job.id)
    assert found is not None and found.id == job.id
    assert (await aggregate_jobs.find_running(group_id)) is None
    # 结果对象**就地更新** → 面板每次轮询拿到的都是实时进度
    assert job.to_dict()["synced"] == 1


async def test_manual_sync_has_no_round_cap(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """**手动触发不限单轮次数**——要搬到台账清空（只有额度墙才停）。

    定时作业仍限 20 次（无人值守的运行时保护）——见下一个用例。
    """
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    master, _source = fake_drivers
    group_id, master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    # 25 个待归拢文件（加上 _setup 建的 1 个 = 26，超过定时作业的 20 次上限）
    async with session_scope() as db_session:
        for i in range(25):
            db_session.add(
                SecondaryStaging(
                    group_id=group_id,
                    source_driver_id=_secondary_id,
                    target_driver_id=master_id,
                    remote_id=f"x{i}",
                    file_name=f"ep{i:02d}.mkv",
                    size_bytes=100,
                    state="pending",
                )
            )
        await db_session.commit()

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.synced == 26, "手动触发应全量归拢，不受 20 次上限"
    assert result.failed == 0
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert all(r.state == "done" for r in rows), "不该有 pending 残留"


async def test_scheduled_sync_cap_comes_from_runtime_param(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """定时作业的单轮上限 = **运行时参数**（面板可调，改完下一轮生效）。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    master, _source = fake_drivers
    group_id, master_id, secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    async with session_scope() as db_session:
        for i in range(8):
            db_session.add(
                SecondaryStaging(
                    group_id=group_id,
                    source_driver_id=secondary_id,
                    target_driver_id=master_id,
                    remote_id=f"y{i}",
                    file_name=f"ep{i:02d}.mkv",
                    size_bytes=100,
                    state="pending",
                )
            )
        await db_session.commit()

    # 参数设为 3 → 本轮只搬 3 个
    monkeypatch.setattr(
        aggregate_sync, "_scheduled_max_submissions", lambda: 3
    )
    result = await aggregate_sync.run_aggregate_sync()
    assert result["synced"] == 3
    assert any("已达最大提交数（3）" in d for d in result["details"])

    # 参数设为 0（不限）→ 剩下的全搬完
    monkeypatch.setattr(
        aggregate_sync, "_scheduled_max_submissions", lambda: None
    )
    result = await aggregate_sync.run_aggregate_sync()
    assert result["synced"] == 6  # _setup 的 1 个 + 8 个里的其余


async def test_manual_sync_ignores_runtime_cap(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """**手动触发不受参数限制**——用户盯着，要搬到台账清空。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    master, _source = fake_drivers
    group_id, master_id, secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    async with session_scope() as db_session:
        for i in range(8):
            db_session.add(
                SecondaryStaging(
                    group_id=group_id,
                    source_driver_id=secondary_id,
                    target_driver_id=master_id,
                    remote_id=f"z{i}",
                    file_name=f"ep{i:02d}.mkv",
                    size_bytes=100,
                    state="pending",
                )
            )
        await db_session.commit()

    # 参数上限很小，但手动触发照旧全量
    monkeypatch.setattr(
        aggregate_sync, "_scheduled_max_submissions", lambda: 1
    )
    result = await aggregate_sync.sync_group_now(group_id)
    assert result.synced == 9  # _setup 的 1 个 + 新增 8 个


async def test_scheduled_sync_keeps_round_cap(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """**定时作业仍限 20 次**（无人值守的运行时保护）——其余留到下一轮。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    master, _source = fake_drivers
    group_id, master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    async with session_scope() as db_session:
        for i in range(25):
            db_session.add(
                SecondaryStaging(
                    group_id=group_id,
                    source_driver_id=_secondary_id,
                    target_driver_id=master_id,
                    remote_id=f"x{i}",
                    file_name=f"ep{i:02d}.mkv",
                    size_bytes=100,
                    state="pending",
                )
            )
        await db_session.commit()

    result = await aggregate_sync.run_aggregate_sync()
    assert result["synced"] == aggregate_sync.MAX_SUBMISSIONS_PER_RUN
    assert any("已达最大提交数" in d for d in result["details"])
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert sum(1 for r in rows if r.state == "pending") == 6


async def test_scheduled_sync_registers_new_files_first(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """定时作业**先登记在途再归拢**——副盘是流动缓冲区，新文件必须先进台账。

    没有这一步的话：数据同步刚传到副盘的文件永远不会被归拢
    （台账里没有它们的行）。
    """
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    class _Fake:
        async def list_dir_by_cid(self, cid, parent_path=""):
            from app.adapters.drivers.base import CloudFile

            return [CloudFile(remote_id="new1", path="/x.mkv", name="x.mkv", size=100)]

    monkeypatch.setattr(aggregate_staging, "build_driver", lambda *a, **k: _Fake())

    result = await aggregate_sync.run_aggregate_sync()
    # 归拢 2 个：_setup 建的 1 个 + 本轮新登记的 1 个
    assert result["synced"] == 2
    rows = await aggregate_staging.list_staging(group_id=group_id)
    states = {r.file_name: r.state for r in rows}
    assert states.get("x.mkv") == "done", "新登记的文件应在本轮被归拢"


async def test_manual_sync_also_registers_new_files(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """**手动触发也要先登记在途**——「全量」应含刚上传的新文件。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    class _Fake:
        async def list_dir_by_cid(self, cid, parent_path=""):
            from app.adapters.drivers.base import CloudFile

            return [CloudFile(remote_id="new9", path="/y.mkv", name="y.mkv", size=100)]

    monkeypatch.setattr(aggregate_staging, "build_driver", lambda *a, **k: _Fake())

    result = await aggregate_sync.sync_group_now(group_id)
    rows = await aggregate_staging.list_staging(group_id=group_id)
    states = {r.file_name: r.state for r in rows}
    assert states.get("y.mkv") == "done", "手动触发也应先登记再归拢"
    assert result.synced == 2  # _setup 的 1 个 + 新登记的 1 个


async def test_scheduled_sync_skips_group_when_locked(
    db, fast_watcher, fake_drivers
) -> None:
    """手动触发正在跑时，定时作业**跳过该组**（不排队干等）。"""
    master, _source = fake_drivers
    group_id, master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    async with session_scope() as db_session:
        db_session.add(
            SecondaryStaging(
                group_id=group_id,
                source_driver_id=_secondary_id,
                target_driver_id=master_id,
                remote_id="x1",
                file_name="a.mkv",
                size_bytes=100,
                state="pending",
            )
        )
        await db_session.commit()

    lock = await aggregate_sync._group_lock(group_id)
    await lock.acquire()
    try:
        result = await aggregate_sync.run_aggregate_sync()
    finally:
        lock.release()
    assert result["synced"] == 0
    assert any("另一轮归拢在跑" in d for d in result["details"])


async def test_small_file_uses_share_transfer_channel(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """小文件走「分享转存」通道——**不消耗云添加额度**（不提交云下载）。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(aggregate_sync, "_share_threshold_bytes", lambda: 1024)
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100
    # 副盘上那个文件的原名（转存落地时用）
    master.source_names["f1"] = "a.mkv"

    result = await aggregate_sync.sync_group_now(group_id)
    # 走了转存：**没有**提交云下载
    assert master.submitted == [], "小文件不该走离线下载（那要消耗额度）"
    assert master.restored, "应发生转存"
    assert result.synced == 1 and result.failed == 0
    # 副盘副本被清理
    assert source.deleted == ["f1"]
    # 台账记下通道
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].transfer_mode == "share_transfer"
    assert rows[0].state == "done"


async def test_big_file_still_uses_cloud_download(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """大文件照旧走离线下载（云下载）——转存只用于小文件。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(aggregate_sync, "_share_threshold_bytes", lambda: 10)
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    result = await aggregate_sync.sync_group_now(group_id)
    assert master.restored == [], "大文件不该走转存"
    assert len(master.submitted) == 1, "大文件应提交云下载"
    assert result.synced == 1
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].transfer_mode == "cloud_download"


async def test_share_transfer_failure_falls_back_to_cloud_download(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """分享通道失败 → **回退离线下载**（保底：吃额度但不死）。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(aggregate_sync, "_share_threshold_bytes", lambda: 1024)
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100
    master.restore_error = "转存失败（模拟）"

    result = await aggregate_sync.sync_group_now(group_id)
    assert len(master.submitted) == 1, "应回退到离线下载"
    assert result.synced == 1 and result.failed == 0
    assert any("回退离线下载" in d for d in result.details)
    # 副盘副本仍被清理
    assert source.deleted == ["f1"]


async def test_share_channel_can_be_disabled(
    db, fast_watcher, fake_drivers, monkeypatch
) -> None:
    """阈值设 0 → 关闭分享通道，全部走离线下载。"""
    monkeypatch.setattr(aggregate_sync, "SUBMIT_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(aggregate_sync, "_share_threshold_bytes", lambda: 0)
    master, _source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    await aggregate_sync.sync_group_now(group_id)
    assert master.restored == []
    assert len(master.submitted) == 1


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


async def test_user_download_file_is_protected(
    db, fast_watcher, fake_drivers
) -> None:
    """目标目录里**原本就有** ``download`` 文件时，用户的文件分毫未动。

    服务端会把我们的落地文件自动改名成 ``download(1)`` → 改名时**跳过**
    用户原有的同名条目、只改我们那份 → 归拢**一次就成功**。

    ⚠️ 曾经的错法：把候选名 ``download`` 整个排除 → ``download(1)`` 也
    认不出来 → **这个目录的归拢必然失败**（已修：排除改为「跳过同名
    条目」，保留 ``download(N)`` 形态）。
    """
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    # 用户原有的 download 文件（字节数恰好一致——最危险的情形）
    master.files["download"] = (100, "user-file")
    master.landed_size = 100

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.synced == 1, "应当一次成功（我们的文件落成了 download(1)）"
    assert result.failed == 0
    # 用户的文件分毫未动
    assert ("user-file", "a.mkv") not in master.renamed, "误改了用户的文件！"
    assert master.files.get("download") == (100, "user-file"), "用户的文件被动了"
    # 我们那份被改名成了目标名
    assert master.files.get("a.mkv") is not None
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "done"
    # 副盘副本也被清理
    assert source.deleted == ["f1"]


async def test_leftover_is_claimed_on_retry(
    db, fast_watcher, fake_drivers
) -> None:
    """上一轮落盘没改名的**残留**，人工重试时被认领（不重复提交）。"""
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    master.landed_size = 100

    # 上一轮的残留：主盘里躺着一个没改名的 download（我们的）
    master.files["download"] = (100, "leftover-1")
    # 该行已经试过一次（attempts >= 1 → 残留认领生效）
    async with session_scope() as db_session:
        row = (
            await db_session.execute(
                select(SecondaryStaging).where(
                    SecondaryStaging.group_id == group_id
                )
            )
        ).scalar_one()
        row.attempts = 1
        row.state = "failed"
        await db_session.commit()

    # failed 行**不自动重试**（失败也扣额度）→ 人工点「重试」才再来
    rows = await aggregate_staging.list_staging(group_id=group_id)
    await aggregate_staging.retry_staging(rows[0].id)

    submitted_before = len(master.submitted)
    result = await aggregate_sync.sync_group_now(group_id)
    assert result.skipped == 1, "应通过认领残留完成，而不是重新提交"
    assert len(master.submitted) == submitted_before, "认领残留不该再提交云下载"
    assert ("leftover-1", "a.mkv") in master.renamed
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "done"
    assert source.deleted == ["f1"]
