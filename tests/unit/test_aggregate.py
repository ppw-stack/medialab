"""网盘聚合（主从）的单元测试。

覆盖**纯逻辑**部分（不碰 DB、不碰网络）：

1. domain 层常量（角色 / 台账状态 / 云添加额度）
2. 额度错误文案识别（撞限额 → 暂停，不硬撞）
3. 落盘名推导（光鸭直链末段 = ``download``）
4. 单文件归拢管线（用假驱动跑完整链路）
"""

from __future__ import annotations

import pytest
from app.domain.aggregate_types import (
    GUANGYA_DAILY_CLOUD_ADD_QUOTA,
    ROLE_LABELS,
    ROLE_MASTER,
    ROLE_SECONDARY,
    STAGING_STATES,
    STAGING_TERMINAL,
    is_valid_role,
)
from app.services.migration_pipeline import is_quota_error, landed_name_from_url
from app.services.migration_pipeline import (
    GUANGYA_LANDED_NAME,
    SubmitGate,
    TargetIndex,
    TaskWatcher,
    norm_dir,
)

# ---------------------------------------------------------------- domain 常量


def test_roles_are_the_single_source() -> None:
    """角色取值与展示名在 domain 层（前后端各写一份必然漂移）。"""
    assert is_valid_role(ROLE_MASTER)
    assert is_valid_role(ROLE_SECONDARY)
    assert not is_valid_role("boss")
    assert not is_valid_role(None)
    assert ROLE_LABELS[ROLE_MASTER] == "主数据源"
    assert ROLE_LABELS[ROLE_SECONDARY] == "副数据源"


def test_cloud_add_quota_is_three_per_day() -> None:
    """光鸭云添加实测 3 次/账号/天——决定归拢速度的唯一硬墙。"""
    assert GUANGYA_DAILY_CLOUD_ADD_QUOTA == 3


def test_staging_states_and_terminal_set() -> None:
    """台账状态齐全，且「终态」只有 done（其余都算未消化完）。"""
    assert "pending" in STAGING_STATES
    assert "syncing" in STAGING_STATES
    assert "paused" in STAGING_STATES
    assert frozenset({"done"}) == STAGING_TERMINAL


# ---------------------------------------------------------------- 错误识别


@pytest.mark.parametrize(
    "message",
    [
        "云添加次数超限，下个自然日刷新",
        "今日额度已用完",
        "配额不足",
        "超过每日上限",
    ],
)
def test_quota_error_markers_are_detected(message: str) -> None:
    """撞额度的文案要能识别——识别后转 paused，不硬撞不重试。"""
    assert is_quota_error(message)


@pytest.mark.parametrize(
    "message",
    ["云添加失败 code=8", "网络超时", "直链为空"],
)
def test_other_errors_are_not_quota(message: str) -> None:
    """普通失败不能被误判成额度耗尽（否则会白白暂停一整天）。"""
    assert not is_quota_error(message)


def testlanded_name_from_url() -> None:
    """光鸭 CDN 直链末段是 ``download``——云下载会按它落名。"""
    assert (
        landed_name_from_url(
            "https://pool-httpdown.guangyacdn.com/download/?fid=abc"
        )
        == "download"
    )
    assert landed_name_from_url("") == ""


def test_norm_dir() -> None:
    """相对目录规范化（去首尾斜杠、合并空段）。"""
    assert norm_dir("/a//b/") == "a/b"
    assert norm_dir("") == ""


# ---------------------------------------------------------------- 管线件


class _FakeGuangya:
    """假光鸭驱动：记录操作，提供可控的目录内容。"""

    def __init__(self, files: dict[str, tuple[str, int]] | None = None) -> None:
        self.files = files if files is not None else {}
        self.created: list[tuple[str, str]] = []
        self.renamed: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.tasks: list[str] = []

    async def create_dir(self, name: str, parent_id: str = "") -> tuple[str, bool]:
        self.created.append((name, parent_id))
        return f"dir-{name}", True

    async def list_files(self, dir_id: str) -> dict[str, int]:
        return {name: size for name, (size, _fid) in self.files.items()}

    async def list_entries(self, dir_id: str) -> dict[str, tuple[int, str]]:
        return {
            name: (size, fid) for name, (size, fid) in self.files.items()
        }

    async def rename_file(self, file_id: str, new_name: str) -> None:
        self.renamed.append((file_id, new_name))

    async def delete_file(self, file_id: str) -> None:
        self.deleted.append(file_id)

    async def create_offline_task(self, url: str, parent_id: str = "") -> str:
        self.tasks.append(url)
        return "task-1"


async def test_target_index_creates_missing_dirs() -> None:
    """目标目录不存在时逐级创建（相对路径）。"""
    gyp = _FakeGuangya()
    index = TargetIndex(gyp, "root")
    dir_id, files = await index.resolve("影视/电影")
    assert dir_id == "dir-电影"
    assert files == {}
    assert [name for name, _parent in gyp.created] == ["影视", "电影"]


async def test_target_index_rename_landed_only_matches_same_size() -> None:
    """认领残留只认**字节数一致**的落地文件（避免误改用户自己的文件）。"""
    gyp = _FakeGuangya({"download": (100, "f1"), "other.mkv": (200, "f2")})
    index = TargetIndex(gyp, "root")
    renamed = await index.rename_landed("", ("download",), 100, "x.mkv")
    assert renamed == "download"
    assert gyp.renamed == [("f1", "x.mkv")]


async def test_target_index_rename_landed_ignores_size_mismatch() -> None:
    """大小不一致 → 不认领（宁可不改，也不改错）。"""
    gyp = _FakeGuangya({"download": (999, "f1")})
    index = TargetIndex(gyp, "root")
    renamed = await index.rename_landed("", ("download",), 100, "x.mkv")
    assert renamed == ""
    assert gyp.renamed == []


async def test_target_index_delete_replaced_requires_exact_size() -> None:
    """删「同名但更小」的目标文件——必须先确认它就是那个大小。"""
    gyp = _FakeGuangya({"x.mkv": (100, "f1")})
    index = TargetIndex(gyp, "root")
    assert await index.delete_replaced("", "x.mkv", 999) is False
    assert gyp.deleted == []
    assert await index.delete_replaced("", "x.mkv", 100) is True
    assert gyp.deleted == ["f1"]


async def test_task_watcher_survives_sequential_waits() -> None:
    """连续多次 ``wait()`` 不能卡死（**轮询协程的竞态回归**）。

    旧实现里轮询协程在「没有在途任务」时退出；若下一个 ``wait()`` 恰好落在
    「协程已决定退出、还没被标记 done」的窗口里，``_ensure_running`` 会以为
    「协程还活着」→ 不新建 → **那个任务永远等不到事件**（wait 永久阻塞）。
    生产里靠文件间 5 秒提交间隔侥幸避开——这是**真实的隐患**，不是测试问题。
    """
    import asyncio

    class _FakeDriver:
        async def list_active_offline_tasks(self) -> list[dict[str, object]]:
            return []  # 任务立刻「消失」

    watcher = TaskWatcher(_FakeDriver(), poll_interval=0.01)
    try:
        for i in range(8):
            await asyncio.wait_for(watcher.wait(f"task-{i}"), timeout=5)
    finally:
        await watcher.close()


async def test_submit_gate_serializes_submissions() -> None:
    """提交闸门保证两次提交之间至少间隔 N 秒（并发 worker 各自 sleep 不够）。"""
    import time

    gate = SubmitGate(0.05)
    start = time.monotonic()
    await gate.wait()
    await gate.wait()
    assert time.monotonic() - start >= 0.04


def test_guangya_landed_name_constant() -> None:
    """光鸭落盘名常量（pipeline 与迁移引擎共用同一份）。"""
    assert GUANGYA_LANDED_NAME == "download"
