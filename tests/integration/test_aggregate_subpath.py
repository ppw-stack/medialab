"""任务级「目标子目录」（**组映射的子路径覆盖**）的集成测试。

## 为什么要有这个字段

目录映射（主盘落点 + 各副盘缓冲根）是**组级**配置——在「网盘聚合」页
配一次即可。但「这个任务传到 ``电影/外语``、那个任务传到 ``电视剧/国产``」
是**任务级**需求，故提供任务级的子路径覆盖。

## 实现口径

子目录落在**副盘缓冲根**下（云端目录被逐层建出来），台账的相对路径
（相对缓冲根）带上前缀 → 归拢引擎「相对结构原样保留到主盘目标目录」
把它带到主盘「归拢目标目录」的同名子目录（**归拢零改动**）。

⚠️ **三个口径必须分清**（本项目最容易踩的坑）：

- **监听口径**：相对本地监听根（``SyncOutcome.rel_path``）
- **缓冲根口径**：相对副盘缓冲根（台账、组级幂等索引）——**带子目录前缀**
- **云端口径**：相对落点目录（``index`` / ``staged_lookup``）——同样带前缀
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.services import (
    aggregate_groups,
    aggregate_staging,
    watch_manager,
)
from app.services import (
    upload_tasks as upload_tasks_service,
)
from app.services.upload_tasks import UploadTaskError
from app.services.uploader import RESULT_UPLOADED, SyncContext

from tests.unit.upload_fakes import FakeDriver, FakeUploader

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _clean_manager():
    """清空管理器状态（落点 / 上下文 / 幂等索引）。"""
    from app.services.upload_tasks import clear_all

    watch_manager._LANDINGS.clear()
    watch_manager._CONTEXTS.clear()
    clear_all()
    yield
    watch_manager._LANDINGS.clear()
    watch_manager._CONTEXTS.clear()
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


def _ctx(
    driver: FakeDriver,
    uploader: FakeUploader,
    root: Path,
    *,
    dir_prefix: str = "",
) -> SyncContext:
    """构造上传上下文（聚合模式：落点 = 副盘缓冲根）。"""
    return SyncContext(
        driver=driver,
        uploader=uploader,
        index=watch_manager.CloudDirIndex(driver, "root-1"),
        root=root,
        delete_retries=2,
        delete_delay=0.01,
        dir_prefix=dir_prefix,
    )


def _landing(group_id: int, driver_id: int, *, sub_path: str = "") -> Any:
    """构造落点状态。"""
    return watch_manager._Landing(
        group_id=group_id,
        driver_id=driver_id,
        driver_name="副号1",
        root_id="root-1",
        root_path="/缓冲1",
        delete_after_upload=False,
        conflict_policy=1,
        sub_path=sub_path,
    )


# ---------------------------------------------------------------- 配置校验


class Test配置校验:
    """任务级目标子目录的配置校验：创建归一化落库、模式冲突与非法路径拒绝。"""

    async def test_创建时归一化并落库(self, db, group, tmp_path) -> None:
        """斜杠归一化后落库。"""
        grp, _master, _s1, _s2 = group
        view = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="/电影//外语/",
        )
        assert view.aggregate_sub_path == "电影/外语"

    async def test_不传就是没有覆盖(self, db, group, tmp_path) -> None:
        """存量行为：不给子目录 = 直接落归拢目标目录根。"""
        grp, _master, _s1, _s2 = group
        view = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
        )
        assert view.aggregate_sub_path is None

    async def test_指定存储源模式传子目录被拒(
        self, db, group, tmp_path
    ) -> None:
        """没有组映射可覆盖 → **显式拒绝**（不是静默忽略）。"""
        _grp, _master, s1, _s2 = group
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=s1.id,
                target_file_id="root-1",
                watch_root=str(tmp_path),
                aggregate_sub_path="电影",
            )
        assert exc.value.status_code == 400
        assert "聚合" in str(exc.value)

    async def test_非法路径被拒(self, db, group, tmp_path) -> None:
        """``..`` 这类路径 → 400。"""
        grp, _master, _s1, _s2 = group
        with pytest.raises(UploadTaskError) as exc:
            await upload_tasks_service.create_task_view(
                name="任务",
                driver_id=None,
                target_file_id="",
                watch_root=str(tmp_path),
                aggregate_group_id=grp.id,
                aggregate_sub_path="../etc",
            )
        assert exc.value.status_code == 400

    async def test_切回指定存储源清空子目录(
        self, db, group, tmp_path
    ) -> None:
        """聚合模式 → 指定存储源：没有组映射可覆盖 → 子目录被清空。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影",
        )
        updated = await upload_tasks_service.update_task_view(
            row.id,
            aggregate_group_id=None,
            driver_id=s1.id,
            target_file_id="root-1",
        )
        assert updated.aggregate_sub_path is None
        assert updated.aggregate_group_id is None

    async def test_改子目录不影响落点快照(self, db, group, tmp_path) -> None:
        """子目录是「缓冲根下的前缀」→ 粘性落点（缓冲根 id）不变。"""
        grp, _master, _s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影",
        )
        updated = await upload_tasks_service.update_task_view(
            row.id, aggregate_sub_path="电视剧/国产"
        )
        assert updated.aggregate_sub_path == "电视剧/国产"
        # 落点仍是副盘缓冲根（不是子目录）——子目录由上传上下文的前缀表达
        assert updated.target_file_id == "root-1"


# ---------------------------------------------------------------- 上传落点


class Test上传落点:
    """上传落点：子目录逐层建出、台账前缀、组级幂等与换盘后前缀保持。"""

    async def test_云端目录逐层建出并落在子目录下(
        self, db, group, tmp_path
    ) -> None:
        """落点 = 副盘缓冲根下的 <子目录>，云端目录被逐层建出来。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影/外语",
        )
        driver = FakeDriver()
        uploader = FakeUploader()
        ctx = _ctx(driver, uploader, tmp_path, dir_prefix="电影/外语")
        watch_manager._CONTEXTS[row.id] = ctx
        landing = _landing(grp.id, s1.id, sub_path="电影/外语")
        landing.staged_index.clear()
        watch_manager._LANDINGS[row.id] = landing

        path = tmp_path / "剧名" / "a.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "剧名/a.mkv", 100
        )
        assert outcome.status == RESULT_UPLOADED

        # 云端目录树：root-1 → 电影 → 外语 → 剧名
        assert "电影" in driver.dirs["root-1"]
        movie_id = driver.dirs["root-1"]["电影"]
        assert "外语" in driver.dirs[movie_id]
        lang_id = driver.dirs[movie_id]["外语"]
        # 落点目录 = 电影/外语/剧名
        parent_id = uploader.uploaded_paths[0][0]
        assert driver.dirs[lang_id]["剧名"] == parent_id

    async def test_台账登记带子目录前缀(self, db, group, tmp_path) -> None:
        """台账 rel_dir = **相对缓冲根**（含子目录前缀）→ 归拢落主盘子目录。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影/外语",
        )
        driver = FakeDriver()
        uploader = FakeUploader()
        ctx = _ctx(driver, uploader, tmp_path, dir_prefix="电影/外语")
        watch_manager._CONTEXTS[row.id] = ctx
        landing = _landing(grp.id, s1.id, sub_path="电影/外语")
        landing.staged_index.clear()
        watch_manager._LANDINGS[row.id] = landing

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status == RESULT_UPLOADED

        rows = await aggregate_staging.list_staging(group_id=grp.id)
        assert len(rows) == 1
        assert rows[0].source_dir == "电影/外语"
        assert rows[0].file_name == "a.mkv"

    async def test_组级幂等命中不重传(self, db, group, tmp_path) -> None:
        """台账里已有（子目录下的）同名同大小 → 不重复上传。"""
        grp, _master, s1, _s2 = group
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影/外语",
        )
        await aggregate_staging.register_uploaded_file(
            group_id=grp.id,
            source_driver_id=s1.id,
            remote_id="fid-old",
            file_name="a.mkv",
            rel_dir="电影/外语",
            size_bytes=100,
        )
        driver = FakeDriver()
        uploader = FakeUploader()
        ctx = _ctx(driver, uploader, tmp_path, dir_prefix="电影/外语")
        watch_manager._CONTEXTS[row.id] = ctx
        landing = _landing(grp.id, s1.id, sub_path="电影/外语")
        landing.staged_index = await aggregate_staging.load_staged_index(grp.id)
        watch_manager._LANDINGS[row.id] = landing
        # 组级幂等探测（挂到上下文上）
        ctx.staged_lookup = watch_manager._make_staged_lookup(landing)

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.staged_hit is True
        assert uploader.uploaded == []

    async def test_换盘后子目录前缀保持(
        self, db, group, tmp_path, monkeypatch
    ) -> None:
        """撞墙换盘 → 新落点的上下文**仍带同一个子目录前缀**。"""
        grp, _master, s1, s2 = group
        first, second = sorted([s1.id, s2.id])
        row = await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影/外语",
        )
        clients = {
            first: (
                FakeDriver(),
                FakeUploader(fail=True, fail_reason="本月上传流量超出限额"),
            ),
            second: (FakeDriver(), FakeUploader()),
        }

        async def _fake_build_clients(driver_id: int):
            return clients[driver_id]

        monkeypatch.setattr(
            watch_manager, "_build_clients", _fake_build_clients
        )

        driver = FakeDriver()
        uploader = FakeUploader(fail=True, fail_reason="本月上传流量超出限额")
        ctx = _ctx(driver, uploader, tmp_path, dir_prefix="电影/外语")
        watch_manager._CONTEXTS[row.id] = ctx
        landing = _landing(grp.id, first, sub_path="电影/外语")
        landing.staged_index.clear()
        watch_manager._LANDINGS[row.id] = landing

        path = tmp_path / "a.mkv"
        path.write_bytes(b"x" * 100)
        outcome = await watch_manager._upload_with_switch(
            row.id, ctx, path, "a.mkv", 100
        )
        assert outcome.status == RESULT_UPLOADED
        # 换盘了，且新上下文的子目录前缀没丢
        assert watch_manager._LANDINGS[row.id].driver_id == second
        assert watch_manager._CONTEXTS[row.id].dir_prefix == "电影/外语"

    async def test_启动参数带子目录(self, db, group, tmp_path, monkeypatch) -> None:
        """重启恢复（``start_all``）不能丢子目录。"""
        grp, _master, _s1, _s2 = group
        await upload_tasks_service.create_task_view(
            name="任务",
            driver_id=None,
            target_file_id="",
            watch_root=str(tmp_path),
            aggregate_group_id=grp.id,
            aggregate_sub_path="电影/外语",
        )
        captured: dict[str, Any] = {}

        async def _fake_start(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(watch_manager, "start_watcher", _fake_start)
        await watch_manager.start_all()
        assert captured["aggregate_sub_path"] == "电影/外语"

    async def test_聚合目标下发归拢目录id(self, db, group) -> None:
        """前端目录浏览器的起点（主盘归拢目标目录 id）要下发。"""
        resp = await upload_tasks_service.aggregate_targets()
        groups = resp["groups"]
        assert len(groups) == 1
        assert groups[0]["target_dir_id"] == "target-dir"
