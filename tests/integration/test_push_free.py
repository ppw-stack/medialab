"""免费种推送集成测试（Phase 0）。

推送链路的业务规则（选种白名单 + 去重 + 幂等）是「该不该推」的核心，
写错了会把非免费种推给用户、或重复推送。

覆盖：
- 选种白名单（只推永久/限时/2X 限时；折扣/未知/退还不推）
- 限时种剩余时间阈值
- 已推过的不再选
- 幂等入队（同一 item 不重复处理）
- 无默认下载器时的降级
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.timeutils import bj_now_dt, format_bj
from app.db.models import DownloaderInstance, TaskQueue, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.factory import AdapterFactory
from app.services.push_free import PushFreeService
from app.services.runtime_params import (
    RuntimeParamManager,
    get_runtime_params,
    set_runtime_params,
)
from app.services.task_queue import TaskQueueService
from sqlalchemy import select
from sqlalchemy.exc import OperationalError


@pytest.fixture(autouse=True)
def _runtime_params() -> None:
    """注入运行时参数（push_free 每次 run 都从这里读）。"""
    mgr = RuntimeParamManager()
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
    mgr.set_memory("push_max_active_downloads", 0)  # 既有测试默认不限水位
    mgr.set_memory("push_download_interval_ms", 0)  # 测试里不等
    mgr.set_memory("monitor_pre_pause_minutes", 10)
    set_runtime_params(mgr)
    yield
    set_runtime_params(None)  # type: ignore[arg-type]


async def _add_downloader(*, is_default: bool = True) -> int:
    async with session_scope() as session:
        dl = DownloaderInstance(
            name="dl1",
            type="qbittorrent",
            url="http://localhost:8081",
            is_default=is_default,
            enabled=True,
        )
        session.add(dl)
        await session.flush()
        return dl.id


async def _add_torrent(
    *,
    free_type: str = FreeType.PERMANENT,
    free_end_at: str | None = None,
    push_status: str | None = None,
    item_id: str = "i1",
    site_id: int = 1,
    last_seen_at: str | None = None,
) -> int:
    async with session_scope() as session:
        t = TorrentIndex(
            site_id=site_id,
            item_id=item_id,
            name=f"torrent-{item_id}",
            free_type=free_type,
            free_end_at=free_end_at,
            push_status=push_status,
            **({"last_seen_at": last_seen_at} if last_seen_at else {}),
        )
        session.add(t)
        await session.flush()
        return t.id


async def _add_site(*, enabled: bool = True) -> int:
    from app.db.models import Site

    async with session_scope() as session:
        s = Site(
            name="s1", domain="s1.test", adapter="generic", enabled=enabled
        )
        session.add(s)
        await session.flush()
        return s.id


def _service() -> PushFreeService:
    return PushFreeService(AdapterFactory())


# ------------------------------------------------------------- 无下载器

async def test_no_default_downloader_is_graceful(db: None) -> None:
    """未配置默认下载器 → 跳过，不抛异常。"""
    result = await _service().run()
    assert result.pushed == 0
    assert "未配置默认下载器" in result.summary


async def test_disabled_default_downloader_is_graceful(db: None) -> None:
    await _add_downloader(is_default=True)
    async with session_scope() as session:
        dl = (await session.execute(select(DownloaderInstance))).scalar_one()
        dl.enabled = False
        await session.flush()

    result = await _service().run()
    assert result.pushed == 0
    assert "未配置默认下载器" in result.summary


# ------------------------------------------------------------- 停用站点

async def test_disabled_site_is_not_selected(db: None) -> None:
    """**停用站点的免费种不推**。

    2026-11-12 prod：站点停用（``sites.enabled=0``）只停止拉取，**不删**
    ``torrent_index`` 里的快照 → 停用后旧快照仍在 24h 新鲜度窗口内被选中，
    出现「站点已停用却还在推对应站点的种子」。候选查询必须 INNER JOIN
    ``sites`` 并过滤 ``enabled``。
    """
    await _add_downloader()
    site_id = await _add_site(enabled=False)
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

    result = await _service().run()
    assert result.selected == 0
    assert result.pushed == 0


async def test_enabled_site_is_still_selected(db: None) -> None:
    """对照组：启用站点的免费种照常入选（别把过滤做过头）。"""
    await _add_downloader()
    site_id = await _add_site(enabled=True)
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

    result = await _service().run()
    assert result.selected == 1


# ------------------------------------------------------------- 选种白名单

async def test_permanent_free_is_selected(db: None) -> None:
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

    result = await _service().run()
    assert result.selected == 1


async def test_stale_snapshot_is_not_selected(db: None) -> None:
    """**陈旧快照不进候选**（凭据失效期间攒下的条目）。

    2026-11-08 prod：ourbits 凭据过期 10 天，1557 条 10 天前的快照仍在
    库里——永久免费种（``free_end_at IS NULL``）**永远入选**旧候选查询，
    一旦别的站候选不够填满 push_max_per_run，就会把「当前实为收费」的
    种子推给下载器。新鲜度下限（24h）把这类条目挡在候选池外。
    """
    await _add_downloader()
    site_id = await _add_site()
    stale = format_bj(bj_now_dt() - timedelta(hours=72))
    await _add_torrent(
        free_type=FreeType.PERMANENT, site_id=site_id, last_seen_at=stale
    )

    result = await _service().run()
    assert result.selected == 0
    assert result.pushed == 0


async def test_timed_free_with_enough_remaining_is_selected(db: None) -> None:
    await _add_downloader()
    site_id = await _add_site()
    end = format_bj(bj_now_dt() + timedelta(hours=48))
    await _add_torrent(free_type=FreeType.TIMED, free_end_at=end, site_id=site_id)

    result = await _service().run()
    assert result.selected == 1


async def test_timed_free_with_insufficient_remaining_is_skipped(db: None) -> None:
    """剩余时间 < push_min_remaining_hours（6h）→ 不选。"""
    await _add_downloader()
    site_id = await _add_site()
    end = format_bj(bj_now_dt() + timedelta(hours=2))
    await _add_torrent(free_type=FreeType.TIMED, free_end_at=end, site_id=site_id)

    result = await _service().run()
    assert result.selected == 0


async def test_expired_timed_free_is_skipped(db: None) -> None:
    await _add_downloader()
    site_id = await _add_site()
    end = format_bj(bj_now_dt() - timedelta(hours=1))
    await _add_torrent(free_type=FreeType.TIMED, free_end_at=end, site_id=site_id)

    result = await _service().run()
    assert result.selected == 0


async def test_timed_free_without_end_at_is_skipped(db: None) -> None:
    """限时种缺 free_end_at → 保守跳过。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.TIMED, free_end_at=None, site_id=site_id)

    result = await _service().run()
    assert result.selected == 0


@pytest.mark.parametrize(
    "free_type",
    [FreeType.NEUTRAL, FreeType.UNKNOWN, FreeType.REFUND],
)
async def test_non_free_types_are_not_pushed(db: None, free_type: str) -> None:
    """折扣/未知/退还种一律不自动推送。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=free_type, site_id=site_id)

    result = await _service().run()
    assert result.selected == 0


async def test_twoup_timed_is_pushed(db: None) -> None:
    """2X 限时在白名单里。"""
    await _add_downloader()
    site_id = await _add_site()
    end = format_bj(bj_now_dt() + timedelta(hours=48))
    await _add_torrent(free_type=FreeType.TWOU_TIMED, free_end_at=end, site_id=site_id)

    result = await _service().run()
    assert result.selected == 1


# --------------------------------------------------------------- 去重

async def test_already_pushed_is_not_selected_again(db: None) -> None:
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(
        free_type=FreeType.PERMANENT,
        push_status=PushStatus.PUSHED,
        site_id=site_id,
    )

    result = await _service().run()
    assert result.selected == 0


async def test_failed_push_is_retried_next_run(db: None) -> None:
    """push_status=failed 的条目下轮仍会被选中（重试语义）。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(
        free_type=FreeType.PERMANENT,
        push_status=PushStatus.FAILED,
        site_id=site_id,
    )

    result = await _service().run()
    assert result.selected == 1


async def test_null_push_status_is_selected(db: None) -> None:
    """push_status 为 NULL = 从未推过 → 应被选中。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, push_status=None, site_id=site_id)

    result = await _service().run()
    assert result.selected == 1


# --------------------------------------------------------------- 幂等

async def test_second_run_skips_already_queued_items(db: None) -> None:
    """第一轮入队后，第二轮幂等跳过（不重复处理）。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

    first = await _service().run()
    assert first.selected == 1

    second = await _service().run()
    assert second.selected == 1
    assert second.skipped_already_pushed == 1


async def test_max_per_run_limits_candidates(db: None) -> None:
    """单次推送上限生效。"""
    await _add_downloader()
    site_id = await _add_site()
    for i in range(5):
        await _add_torrent(
            free_type=FreeType.PERMANENT, item_id=f"i{i}", site_id=site_id
        )

    mgr = get_runtime_params()
    mgr.set_memory("push_max_per_run", 2)

    result = await _service().run()
    assert result.selected == 2


# ------------------------------------------------------------------ 步骤

async def test_steps_are_reported(db: None) -> None:
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

    result = await _service().run()
    names = [s.name for s in result.steps]
    assert "查询默认下载器" in names
    assert "查询候选免费种" in names
    assert "下载 .torrent" in names
    assert "推送到下载器" in names


async def test_no_candidates_reports_skip_step(db: None) -> None:
    await _add_downloader()
    result = await _service().run()
    assert result.selected == 0
    assert "无新候选" in result.summary


async def test_steps_含逐种明细(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """推送步骤带**逐种明细**——回答「推了哪些 / 哪些失败」。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id,
                       item_id="i1")

    # 执行器打桩——真实推送要连站点 + 下载器，本用例只关心明细结构
    async def _fake_execute(payload, factory, *, category="auto-push"):
        return {"info_hash": "a" * 40, "name": "torrent-i1", "idempotent": False}

    monkeypatch.setattr("app.services.task_executor.execute_add_torrent",
                        _fake_execute)

    result = await _service().run()

    push_step = next(s for s in result.steps if s.name == "推送到下载器")
    assert push_step.items is not None
    assert len(push_step.items) == 1
    item = push_step.items[0]
    assert item.name == "torrent-i1"
    assert item.status == "ok"
    assert "推送成功" in (item.detail or "")


async def test_steps_明细记录推送失败(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """推送失败 → 明细是 fail 状态 + 带失败原因。"""
    from app.services.task_executor import PushItemError

    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id,
                       item_id="i1")

    async def _fail_execute(payload, factory, *, category="auto-push"):
        raise PushItemError("download", "下载超时")

    monkeypatch.setattr("app.services.task_executor.execute_add_torrent",
                        _fail_execute)

    result = await _service().run()

    push_step = next(s for s in result.steps if s.name == "推送到下载器")
    assert push_step.items is not None
    item = push_step.items[0]
    assert item.status == "fail"
    assert "下载超时" in (item.detail or "")


async def test_steps_候选步骤含明细(db: None) -> None:
    """候选步骤也带明细（用户能看到推了哪些种、什么免费类型）。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id,
                       item_id="i1")

    result = await _service().run()

    cand_step = next(s for s in result.steps if s.name == "查询候选免费种")
    assert cand_step.items is not None
    assert cand_step.items[0].name == "torrent-i1"


def _make_locked_error() -> OperationalError:
    """构造一个「撞 SQLite 写锁」的异常（与生产报错同形态）。"""
    return OperationalError(
        "INSERT INTO task_queue (...)",
        None,
        Exception("database is locked"),
    )


class TestEnqueueLockContention:
    """入队撞 SQLite 写锁时的行为（2026-11-12 prod 事故回归）。

    事故：扫描作业批量写库期间持有写锁 → ``INSERT INTO task_queue``
    等满 ``busy_timeout``(10s) 仍失败 → **整轮推送炸掉**（手动触发
    直接报 ``database is locked``）。

    契约：入队/认领是**核心业务**，不能静默放弃 → 退避重试；重试耗尽
    则该候选记失败明细、**整轮不炸**（下轮 cron 按幂等键自动补推）。
    """

    async def test_transient_lock_on_enqueue_is_retried(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """入队撞一次写锁 → 退避重试后成功（推送不丢）。"""
        await _add_downloader()
        site_id = await _add_site()
        await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

        async def _fake_execute(payload, factory, *, category="auto-push"):
            return {"info_hash": "a" * 40, "name": "t-i1", "idempotent": False}

        monkeypatch.setattr(
            "app.services.task_executor.execute_add_torrent", _fake_execute
        )

        calls = {"n": 0}
        real_enqueue = TaskQueueService.enqueue

        async def _flaky_enqueue(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _make_locked_error()
            return await real_enqueue(self, *args, **kwargs)

        monkeypatch.setattr(TaskQueueService, "enqueue", _flaky_enqueue)

        result = await _service().run()

        assert calls["n"] == 2, "撞锁后没重试"
        assert result.pushed == 1, "推送被锁竞争吞掉"
        assert result.failed == 0
        async with session_scope() as session:
            rows = (await session.execute(select(TaskQueue))).scalars().all()
            assert len(rows) == 1, "重试造成重复入队"

    async def test_transient_lock_on_claim_is_retried(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """认领撞一次写锁 → 退避重试后成功。"""
        await _add_downloader()
        site_id = await _add_site()
        await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

        async def _fake_execute(payload, factory, *, category="auto-push"):
            return {"info_hash": "a" * 40, "name": "t-i1", "idempotent": False}

        monkeypatch.setattr(
            "app.services.task_executor.execute_add_torrent", _fake_execute
        )

        calls = {"n": 0}
        real_claim = TaskQueueService.claim_one

        async def _flaky_claim(self, task_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _make_locked_error()
            return await real_claim(self, task_id)

        monkeypatch.setattr(TaskQueueService, "claim_one", _flaky_claim)

        result = await _service().run()

        assert calls["n"] == 2, "撞锁后没重试"
        assert result.pushed == 1
        assert result.failed == 0

    async def test_persistent_lock_fails_one_item_and_keeps_going(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重试耗尽 → 该候选记失败明细，**整轮不炸**，其它候选照常推。"""
        await _add_downloader()
        site_id = await _add_site()
        await _add_torrent(
            free_type=FreeType.PERMANENT, item_id="locked", site_id=site_id
        )
        await _add_torrent(
            free_type=FreeType.PERMANENT, item_id="ok", site_id=site_id
        )

        async def _fake_execute(payload, factory, *, category="auto-push"):
            return {"info_hash": "a" * 40, "name": "t", "idempotent": False}

        monkeypatch.setattr(
            "app.services.task_executor.execute_add_torrent", _fake_execute
        )

        real_enqueue = TaskQueueService.enqueue

        async def _locked_for_one(
            self, kind, payload, *, idem_key, run_at=None
        ):
            if "locked" in idem_key:
                raise _make_locked_error()
            return await real_enqueue(
                self, kind, payload, idem_key=idem_key, run_at=run_at
            )

        monkeypatch.setattr(TaskQueueService, "enqueue", _locked_for_one)

        result = await _service().run()

        assert result.failed == 1, "撞锁候选应记失败"
        assert result.pushed == 1, "其它候选应照常推送（单点不炸整轮）"
        push_step = next(s for s in result.steps if s.name == "推送到下载器")
        assert push_step.items is not None
        details = [i.detail or "" for i in push_step.items]
        assert any("数据库忙" in d for d in details), "明细没记撞锁原因"
        # 失败的候选**不写** push_status（也不写 failed）→ 下轮 cron
        # 仍会选中并补推
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(TorrentIndex).where(
                            TorrentIndex.item_id.in_(("locked", "ok"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_item = {r.item_id: r.push_status for r in rows}
            assert by_item["locked"] is None
            # 只有成功的候选入了队（撞锁候选没进队列）
            qrows = (await session.execute(select(TaskQueue))).scalars().all()
            assert len(qrows) == 1


# ============================================================ 动态水位配额


class TestDynamicCapacity:
    """按默认下载器在途下载数动态计算本次推送配额。"""

    async def test_pushes_only_available_slots(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """在途 7 / 上限 10，单次上限 20，候选 5 个 → 只推 3 个。"""
        await _add_downloader()
        site_id = await _add_site()
        for i in range(5):
            await _add_torrent(
                free_type=FreeType.PERMANENT, item_id=f"cap-{i}", site_id=site_id
            )
        get_runtime_params().set_memory("push_max_active_downloads", 10)
        get_runtime_params().set_memory("push_max_per_run", 20)

        async def _fake_execute(payload, factory, *, category="auto-push"):
            return {"info_hash": "a" * 40, "name": "t", "idempotent": False}

        monkeypatch.setattr(
            "app.services.task_executor.execute_add_torrent", _fake_execute
        )

        result = await _service().run(active_downloads=7)
        assert result.selected == 3
        assert result.pushed == 3
        assert result.active_downloads == 7
        assert result.max_active_downloads == 10
        assert result.skipped_by_capacity is False
        assert any(s.name == "检查下载器水位" for s in result.steps)

    async def test_skips_when_at_capacity(self, db: None) -> None:
        """在途 10 / 上限 10 → 跳过推送，标记 skipped_by_capacity=True。"""
        await _add_downloader()
        site_id = await _add_site()
        await _add_torrent(
            free_type=FreeType.PERMANENT, item_id="cap-full", site_id=site_id
        )
        get_runtime_params().set_memory("push_max_active_downloads", 10)

        result = await _service().run(active_downloads=10)
        assert result.selected == 0
        assert result.pushed == 0
        assert result.skipped_by_capacity is True
        assert "已达上限暂缓推送" in result.summary

    async def test_queries_downloader_when_active_downloads_not_passed(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未传入 active_downloads 时主动查下载器，仅计未完成活跃下载任务。"""
        from app.adapters.downloaders.base import TorrentInfo, TorrentState

        await _add_downloader()
        site_id = await _add_site()
        for i in range(4):
            await _add_torrent(
                free_type=FreeType.PERMANENT, item_id=f"q-{i}", site_id=site_id
            )
        get_runtime_params().set_memory("push_max_active_downloads", 5)

        class _FakeDl:
            async def list_torrents(self) -> list[TorrentInfo]:
                return [
                    TorrentInfo(hash="1" * 40, name="dl", state=TorrentState.DOWNLOADING, size=100, completed_bytes=10, category="auto-push"),
                    TorrentInfo(hash="2" * 40, name="st", state=TorrentState.STALLED_DL, size=100, completed_bytes=0, category="auto-push"),
                    TorrentInfo(hash="3" * 40, name="qd", state=TorrentState.DOWNLOADING, size=100, completed_bytes=0, is_queued=True, category="manual-push"),
                    # 以下不计入在途下载：非系统分类 / 无分类 / 已完成 / 已暂停 / 报错
                    TorrentInfo(hash="7" * 40, name="ext-cat", state=TorrentState.STALLED_DL, size=100, completed_bytes=10, category="国产剧"),
                    TorrentInfo(hash="8" * 40, name="no-cat", state=TorrentState.DOWNLOADING, size=100, completed_bytes=10, category=None),
                    TorrentInfo(hash="4" * 40, name="seed", state=TorrentState.SEEDING, size=100, completed_bytes=100, category="auto-push"),
                    TorrentInfo(hash="5" * 40, name="paused", state=TorrentState.PAUSED_DL, size=100, completed_bytes=20, category="auto-push"),
                    TorrentInfo(hash="6" * 40, name="err", state=TorrentState.ERROR, size=100, completed_bytes=20, category="auto-push"),
                ]

        factory = AdapterFactory()
        monkeypatch.setattr(factory, "downloader", lambda _dl: _FakeDl())

        async def _fake_execute(payload, factory, *, category="auto-push"):
            return {"info_hash": "a" * 40, "name": "t", "idempotent": False}

        monkeypatch.setattr(
            "app.services.task_executor.execute_add_torrent", _fake_execute
        )

        result = await PushFreeService(factory).run()
        # 在途 3 / 上限 5 → 推 2 个
        assert result.active_downloads == 3
        assert result.selected == 2
        assert result.pushed == 2

    async def test_downloader_query_failure_does_not_blindly_push(
        self, db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """水位查询失败时安全降级（不盲推）。"""
        await _add_downloader()
        site_id = await _add_site()
        await _add_torrent(
            free_type=FreeType.PERMANENT, item_id="fail-dl", site_id=site_id
        )
        get_runtime_params().set_memory("push_max_active_downloads", 10)

        class _BoomDl:
            async def list_torrents(self) -> list[object]:
                raise RuntimeError("qb timeout")

        factory = AdapterFactory()
        monkeypatch.setattr(factory, "downloader", lambda _dl: _BoomDl())

        result = await PushFreeService(factory).run()
        assert result.pushed == 0
        assert result.failed == 1
        assert "查询下载器在途任务失败" in result.summary

