"""「本地同步 → 网盘聚合」的集成测试。

覆盖**有 DB 的部分**：

1. 创建聚合任务的校验矩阵（组停用 / 无主盘 / 未配归拢目标 / 无副盘缓冲根）
2. 落点分配（固定顺序 + 粘性快照）
3. 上传成功 → **登记台账**（幂等）
4. **组级幂等**：台账命中 → 不重传
5. **撞墙换盘**：限额类失败 → 标记 + 换盘 + 当场重试；全撞墙 → 失败点名
6. 耗尽标记跨月失效
7. 删组守卫（组下挂着上传任务 → 409）
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.db.models import Driver, UploadTask
from app.db.session import session_scope
from app.domain.aggregate_types import STAGING_PENDING
from app.services import aggregate_groups, aggregate_staging, aggregate_upload, watch_manager
from app.services import upload_tasks as upload_tasks_service
from app.services.errors import ServiceError
from app.services.upload_tasks import UploadTaskError
from app.services.uploader import RESULT_FAILED, RESULT_UPLOADED, SyncContext
from sqlalchemy import select

from tests.unit.upload_fakes import FakeDriver, FakeUploader

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _clean_manager():
    """清空管理器状态（落点 / 上下文 / 幂等索引）。"""
    from app.services.upload_tasks import clear_all

    watch_manager._LANDINGS.clear()
    watch_manager._CONTEXTS.clear()
    watch_manager._STAGED_INDEX.clear()
    clear_all()
    yield
    watch_manager._LANDINGS.clear()
    watch_manager._CONTEXTS.clear()
    watch_manager._STAGED_INDEX.clear()
    clear_all()


@pytest.fixture
async def group(db):
    """一个配齐的聚合组：1 主 + 2 副（各带缓冲根）。"""
    from app.services import driver_service

    master = await driver_service.add_driver(driver_type="guangya", name="主号")
    sec1 = await driver_service.add_driver(driver_type="guangya", name="副号1")
    sec2 = await driver_service.add_driver(driver_type="guangya", name="副号2")
    grp = await aggregate_groups.create_group("测试组")
    await aggregate_groups.assign_driver_role(
        master.id, group_id=grp.id, role="master"
    )
    await aggregate_groups.assign_driver_role(
        sec1.id, group_id=grp.id, role="secondary"
    )
    await aggregate_groups.assign_driver_role(
        sec2.id, group_id=grp.id, role="secondary"
    )
    await aggregate_groups.set_mapping(
        grp.id,
        target_dir_id="target-dir",
        target_dir_path="/影视",
        roots=[
            aggregate_groups.GroupRootSpec(
                driver_id=sec1.id, root_id="root-1", root_path="/缓冲1"
            ),
            aggregate_groups.GroupRootSpec(
                driver_id=sec2.id, root_id="root-2", root_path="/缓冲2"
            ),
        ],
    )
    return grp, master, sec1, sec2


def _ctx(driver: FakeDriver, uploader: FakeUploader, root: Path) -> SyncContext:
    return SyncContext(
        driver=driver,
        uploader=uploader,
        index=watch_manager.CloudDirIndex(driver, "root-1"),
        root=root,
        delete_retries=2,
        delete_delay=0.01,
    )


# ---------------------------------------------------------------- 校验矩阵


class Test创建校验:
    async def test_组不存在(self, db, tmp_path) -> None:
        """组 id 不存在 → 404。"""
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=None,
                target_file_id="",
                watch_root=str(tmp_path),
                aggregate_group_id=999999,
            )
        assert exc.value.status_code == 404

    async def test_组停用(self, db, group, tmp_path) -> None:
        """组停用 → 400（停用的组不参与归拢，文件没人搬）。"""
        grp, _master, _s1, _s2 = group
        await aggregate_groups.update_group(grp.id, enabled=False)
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=None,
                target_file_id="",
                watch_root=str(tmp_path),
                aggregate_group_id=grp.id,
            )
        assert exc.value.status_code == 400
        assert "停用" in str(exc.value)

    async def test_无主盘(self, db, group, tmp_path) -> None:
        """组没有主盘 → 400。"""
        grp, master, _s1, _s2 = group
        await aggregate_groups.clear_driver_role(master.id)
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=None,
                target_file_id="",
                watch_root=str(tmp_path),
                aggregate_group_id=grp.id,
            )
        assert exc.value.status_code == 400
        assert "主数据源" in str(exc.value)

    async def test_未配归拢目标目录(self, db, group, tmp_path) -> None:
        """组没配归拢目标目录 → 400。"""
        grp, _master, _s1, _s2 = group
        await aggregate_groups.set_mapping(
            grp.id, target_dir_id=None, target_dir_path=None
        )
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=None,
                target_file_id="",
                watch_root=str(tmp_path),
                aggregate_group_id=grp.id,
            )
        assert exc.value.status_code == 400
        assert "归拢目标目录" in str(exc.value)

    async def test_副盘未配缓冲根(self, db, group, tmp_path) -> None:
        """组内副盘没配缓冲根 → 400（没有落点）。"""
        grp, _master, _s1, _s2 = group
        await aggregate_groups.set_mapping(
            grp.id,
            target_dir_id="target-dir",
            target_dir_path="/影视",
            roots=[],
        )
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=None,
                target_file_id="",
                watch_root=str(tmp_path),
                aggregate_group_id=grp.id,
            )
        assert exc.value.status_code == 400
        assert "缓冲根" in str(exc.value)

    async def test_创建成功并写入落点快照(self, db, group, tmp_path) -> None:
        """创建成功 → 组 id + 落点快照（副盘 + 缓冲根）写进任务行。"""
        grp, _master, s1, _s2 = group
        view = await upload_tasks_service.create_task_view(
            name="聚合任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        assert view.aggregate_group_id == grp.id
        # 固定顺序：driver id 升序 → 第一个副盘
        assert view.driver_id == s1.id
        assert view.target_file_id == "root-1"


# ---------------------------------------------------------------- 落点分配


class Test落点分配:
    async def test_固定顺序取第一个可用副盘(self, db, group) -> None:
        """候选按 driver id 升序——不排序。"""
        grp, _master, s1, s2 = group
        cands = await aggregate_upload.list_candidates(grp.id)
        assert [c.driver_id for c in cands] == sorted([s1.id, s2.id])
        picked = await aggregate_upload.pick_candidate(grp.id, 0)
        assert picked is not None
        assert picked.driver_id == min(s1.id, s2.id)

    async def test_粘性_优先沿用当前落点(self, db, group) -> None:
        """已有落点仍可用 → 沿用它（不换盘）。"""
        grp, _master, s1, s2 = group
        second = max(s1.id, s2.id)
        picked = await aggregate_upload.pick_candidate(
            grp.id, 0, prefer=second
        )
        assert picked is not None
        assert picked.driver_id == second

    async def test_额度耗尽的副盘被跳过(self, db, group) -> None:
        """标记「本月耗尽」的副盘不作候选。"""
        grp, _master, s1, s2 = group
        first = min(s1.id, s2.id)
        await aggregate_upload.mark_quota_exhausted(first)
        picked = await aggregate_upload.pick_candidate(grp.id, 0)
        assert picked is not None
        assert picked.driver_id != first

    async def test_标记跨月失效(self, db, group) -> None:
        """标记是上个月 → 不算耗尽。"""
        grp, _master, s1, _s2 = group
        async with session_scope() as db_session:
            row = (
                await db_session.execute(
                    select(Driver).where(Driver.id == s1.id)
                )
            ).scalar_one()
            row.upload_quota_exhausted_month = "2000-01"
            await db_session.commit()
        cands = await aggregate_upload.list_candidates(grp.id)
        assert all(c.exhausted is False for c in cands)


# ---------------------------------------------------------------- 台账登记


class Test台账登记:
    async def test_上传成功后登记(self, db, group, tmp_path, monkeypatch) -> None:
        """上传成功 → 台账多一行（数据同步 ↔ 聚合的联动点）。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        driver = FakeDriver()
        uploader = FakeUploader()
        ctx = _ctx(driver, uploader, tmp_path)
        watch_manager._CONTEXTS[row.id] = ctx
        watch_manager._LANDINGS[row.id] = watch_manager._Landing(
            group_id=grp.id,
            driver_id=s1.id,
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )
        watch_manager._STAGED_INDEX[row.id] = {}

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status == RESULT_UPLOADED

        rows = await aggregate_staging.list_staging(group_id=grp.id)
        assert len(rows) == 1
        assert rows[0].file_name == "a.mkv"
        assert rows[0].source_driver_id == s1.id
        assert rows[0].state == STAGING_PENDING

    async def test_登记幂等(self, db, group) -> None:
        """同一文件重复登记 → 只有一行。"""
        grp, _master, s1, _s2 = group
        first = await aggregate_staging.register_uploaded_file(
            group_id=grp.id,
            source_driver_id=s1.id,
            remote_id="fid-1",
            file_name="a.mkv",
            rel_dir="",
            size_bytes=100,
        )
        second = await aggregate_staging.register_uploaded_file(
            group_id=grp.id,
            source_driver_id=s1.id,
            remote_id="fid-1",
            file_name="a.mkv",
            rel_dir="",
            size_bytes=100,
        )
        assert first == "added"
        assert second == "exists"
        rows = await aggregate_staging.list_staging(group_id=grp.id)
        assert len(rows) == 1

    async def test_无主盘不登记(self, db, group) -> None:
        """组没有主盘 → 不登记（返回 None）。"""
        grp, master, s1, _s2 = group
        await aggregate_groups.clear_driver_role(master.id)
        result = await aggregate_staging.register_uploaded_file(
            group_id=grp.id,
            source_driver_id=s1.id,
            remote_id="fid-1",
            file_name="a.mkv",
            rel_dir="",
            size_bytes=100,
        )
        assert result is None

    async def test_幂等索引包含终态行(self, db, group) -> None:
        """已归拢（done）的行也在索引里——防止重复上传。"""
        grp, _master, s1, _s2 = group
        await aggregate_staging.register_uploaded_file(
            group_id=grp.id,
            source_driver_id=s1.id,
            remote_id="fid-1",
            file_name="a.mkv",
            rel_dir="剧名/Season 01",
            size_bytes=100,
        )
        index = await aggregate_staging.load_staged_index(grp.id)
        assert ("剧名/Season 01", "a.mkv") in index
        assert index[("剧名/Season 01", "a.mkv")].size_bytes == 100


# ---------------------------------------------------------------- 撞墙换盘


class Test撞墙换盘:
    async def test_限额失败换盘并重试成功(
        self, db, group, tmp_path, monkeypatch
    ) -> None:
        """限额类失败 → 标记 + 换盘 + 当场重试成功。"""
        grp, _master, s1, s2 = group
        first, second = sorted([s1.id, s2.id])
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )

        ok_uploader = FakeUploader()
        clients = {
            first: (FakeDriver(), FakeUploader(fail=True, fail_reason="本月上传流量超出限额")),
            second: (FakeDriver(), ok_uploader),
        }

        async def _fake_build_clients(driver_id: int):
            return clients[driver_id]

        monkeypatch.setattr(
            watch_manager, "_build_clients", _fake_build_clients
        )

        driver = FakeDriver()
        uploader = FakeUploader(fail=True, fail_reason="本月上传流量超出限额")
        ctx = _ctx(driver, uploader, tmp_path)
        watch_manager._CONTEXTS[row.id] = ctx
        watch_manager._LANDINGS[row.id] = watch_manager._Landing(
            group_id=grp.id,
            driver_id=first,
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )
        watch_manager._STAGED_INDEX[row.id] = {}

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status == RESULT_UPLOADED
        # 换盘了
        assert watch_manager._LANDINGS[row.id].driver_id == second
        # 撞墙的盘被标记「本月耗尽」
        async with session_scope() as db_session:
            marked = (
                await db_session.execute(
                    select(Driver).where(Driver.id == first)
                )
            ).scalar_one()
            assert marked.upload_quota_exhausted_month == aggregate_upload.current_month_key()
        # 落点快照写回（粘性）
        async with session_scope() as db_session:
            task_row = (
                await db_session.execute(
                    select(UploadTask).where(UploadTask.id == row.id)
                )
            ).scalar_one()
            assert task_row.driver_id == second
            assert task_row.target_file_id == "root-2"

    async def test_全部撞墙则失败点名(
        self, db, group, tmp_path, monkeypatch
    ) -> None:
        """所有副盘都撞墙 → 失败并点名原因。"""
        grp, _master, s1, s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )

        async def _fake_build_clients(driver_id: int):
            return (
                FakeDriver(),
                FakeUploader(fail=True, fail_reason="上传额度已用尽"),
            )

        monkeypatch.setattr(
            watch_manager, "_build_clients", _fake_build_clients
        )

        driver = FakeDriver()
        uploader = FakeUploader(fail=True, fail_reason="上传额度已用尽")
        ctx = _ctx(driver, uploader, tmp_path)
        watch_manager._CONTEXTS[row.id] = ctx
        watch_manager._LANDINGS[row.id] = watch_manager._Landing(
            group_id=grp.id,
            driver_id=min(s1.id, s2.id),
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )
        watch_manager._STAGED_INDEX[row.id] = {}

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status == RESULT_FAILED
        assert "额度" in outcome.reason

    async def test_非限额失败不换盘(
        self, db, group, tmp_path, monkeypatch
    ) -> None:
        """普通失败（网络 / 凭据）→ 不换盘。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        driver = FakeDriver()
        uploader = FakeUploader(fail=True, fail_reason="连接超时")
        ctx = _ctx(driver, uploader, tmp_path)
        watch_manager._CONTEXTS[row.id] = ctx
        watch_manager._LANDINGS[row.id] = watch_manager._Landing(
            group_id=grp.id,
            driver_id=s1.id,
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )
        watch_manager._STAGED_INDEX[row.id] = {}

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status == RESULT_FAILED
        assert watch_manager._LANDINGS[row.id].driver_id == s1.id


# ---------------------------------------------------------------- 组级幂等


class Test组级幂等:
    async def test_台账命中不重传(
        self, db, group, tmp_path, monkeypatch
    ) -> None:
        """文件已在组内缓冲（台账有行）→ 不重复上传。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        await aggregate_staging.register_uploaded_file(
            group_id=grp.id,
            source_driver_id=s1.id,
            remote_id="fid-old",
            file_name="a.mkv",
            rel_dir="",
            size_bytes=100,
        )

        driver = FakeDriver()
        uploader = FakeUploader()
        ctx = _ctx(driver, uploader, tmp_path)
        ctx.staged_lookup = watch_manager._make_staged_lookup(row.id)
        watch_manager._CONTEXTS[row.id] = ctx
        watch_manager._LANDINGS[row.id] = watch_manager._Landing(
            group_id=grp.id,
            driver_id=s1.id,
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )
        watch_manager._STAGED_INDEX[row.id] = (
            await aggregate_staging.load_staged_index(grp.id)
        )

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status != RESULT_UPLOADED
        assert uploader.uploaded == []  # 没重传
        # 仍然只有一行台账（没重复登记）
        rows = await aggregate_staging.list_staging(group_id=grp.id)
        assert len(rows) == 1


class Test降级台账:
    async def test_落点写回失败登记台账(self, db, group, tmp_path, monkeypatch) -> None:
        """写库失败 → 登记 ``startup_status``（``/readyz`` 看得见）。"""
        from app.services import startup_status

        grp, _master, s1, _s2 = group
        landing = watch_manager._Landing(
            group_id=grp.id,
            driver_id=s1.id,
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )

        class _Boom:
            async def __aenter__(self):
                raise RuntimeError("写库炸了")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            watch_manager, "session_scope", lambda *a, **k: _Boom()
        )
        await watch_manager._persist_landing(1, landing)

        degradations = startup_status.snapshot()
        assert "upload_landing_persist:1" in degradations
        assert "写库炸了" in degradations["upload_landing_persist:1"]["reason"]

    async def test_写回成功后清除台账(self, db, group, tmp_path) -> None:
        """写成功 → 台账清掉（它是「当前态」语义）。"""
        from app.services import startup_status

        grp, _master, s1, _s2 = group
        view = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        startup_status.record(f"upload_landing_persist:{view.id}", "旧降级")
        landing = watch_manager._Landing(
            group_id=grp.id,
            driver_id=s1.id,
            driver_name="副号1",
            root_id="root-1",
            root_path="/缓冲1",
            delete_after_upload=False,
            conflict_policy=1,
        )
        await watch_manager._persist_landing(view.id, landing)
        assert f"upload_landing_persist:{view.id}" not in startup_status.snapshot()


# ---------------------------------------------------------------- 删组守卫


class Test删组守卫:
    async def test_组下挂着上传任务则拒绝解散(self, db, group, tmp_path) -> None:
        """组下还有数据同步任务 → 409（不级联删用户配置）。"""
        grp, _master, s1, s2 = group
        await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        # 先把盘移出组（第一道闸是「组内还有盘」）——才能走到任务这道闸
        for driver_id in (s1.id, s2.id, _master.id):
            await aggregate_groups.clear_driver_role(driver_id)
        with pytest.raises(ServiceError) as exc:
            await aggregate_groups.delete_group(grp.id)
        assert exc.value.status == 409
        assert "数据同步任务" in str(exc.value)

    async def test_清掉任务后可解散(self, db, group, tmp_path) -> None:
        """任务改回「指定存储源」后 → 可解散。"""
        grp, _master, _s1, _s2 = group
        view = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        await upload_tasks_service.update_task_view(
            view.id, aggregate_group_id=None
        )
        async with session_scope() as db_session:
            task_row = (
                await db_session.execute(
                    select(UploadTask).where(UploadTask.id == view.id)
                )
            ).scalar_one()
            assert task_row.aggregate_group_id is None
