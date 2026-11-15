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
        # 模拟云下载落盘：文件名取自 URL 末段（光鸭直链末段 = download）；
        # 目录里已有同名文件时，服务端自动改名 download(1)、download(2)…
        name = "download"
        n = 1
        while name in self.files:
            name = f"download({n})"
            n += 1
        self.files[name] = (self.landed_size, f"landed-{n}")
        return f"task-{n}"

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
    assert job.synced == 1 and job.cleaned == 1
    assert any("已归拢" in line for line in job.logs)

    # 任务表可查
    found = await aggregate_jobs.get_job(job.id)
    assert found is not None and found.id == job.id
    assert (await aggregate_jobs.find_running(group_id)) is None


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


async def test_user_download_file_protected_then_leftover_claimed(
    db, fast_watcher, fake_drivers
) -> None:
    """用户原有的 ``download`` 文件受保护，而**我们自己的残留**能被认领。

    两阶段契约（2026-11-15 审查修复）：

    - **首次处理**（从未提交过）：目录里的 ``download`` 只可能是用户的
      → 不认领；提交后服务端落成 ``download(1)``，改名候选排除了用户
      的 ``download`` → 校验不过 → 记 failed，**用户的文件分毫未动**
    - **人工重试**（提交过）：``download(1)`` 是我们的残留 → 认领并改名
      → 归拢完成（不重复提交、不再白扣额度）
    """
    master, source = fake_drivers
    group_id, _master_id, _secondary_id = await _setup(db, size=100)
    # 用户原有的 download 文件（字节数恰好一致——最危险的情形）
    master.files["download"] = (100, "user-file")
    master.landed_size = 100

    # —— 第一阶段：首次处理，用户的文件必须分毫未动 ——
    result = await aggregate_sync.sync_group_now(group_id)
    assert result.failed == 1
    assert ("user-file", "a.mkv") not in master.renamed, "误改了用户的文件！"
    assert master.files.get("download") == (100, "user-file"), "用户的文件被动了"
    # 残留（我们那份 download(1)）留在主盘
    assert master.files.get("download(1)") == (100, "landed-2")

    # —— 第二阶段：人工重试，残留被认领（不重复提交） ——
    submitted_before = len(master.submitted)
    rows = await aggregate_staging.list_staging(group_id=group_id)
    await aggregate_staging.retry_staging(rows[0].id)

    result = await aggregate_sync.sync_group_now(group_id)
    assert result.skipped == 1, "应通过认领残留完成，而不是重新提交"
    assert len(master.submitted) == submitted_before, "认领残留不该再提交云下载"
    # 残留（fileId=landed-2，即 download(1)）被改名；用户的文件（user-file）没动
    assert ("landed-2", "a.mkv") in master.renamed
    assert ("user-file", "a.mkv") not in master.renamed, "误改了用户的文件！"
    assert master.files.get("download") == (100, "user-file"), "用户的文件被动了"
    rows = await aggregate_staging.list_staging(group_id=group_id)
    assert rows[0].state == "done"
    # 副盘副本也被清理
    assert source.deleted == ["f1"]
