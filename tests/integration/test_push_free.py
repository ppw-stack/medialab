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
from app.db.models import DownloaderInstance, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType, PushStatus
from app.services.factory import AdapterFactory
from app.services.push_free import PushFreeService
from app.services.runtime_params import (
    RuntimeParamManager,
    get_runtime_params,
    set_runtime_params,
)
from sqlalchemy import select


@pytest.fixture(autouse=True)
def _runtime_params() -> None:
    """注入运行时参数（push_free 每次 run 都从这里读）。"""
    mgr = RuntimeParamManager()
    mgr.set_memory("push_min_remaining_hours", 6)
    mgr.set_memory("push_max_per_run", 20)
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
) -> int:
    async with session_scope() as session:
        t = TorrentIndex(
            site_id=site_id,
            item_id=item_id,
            name=f"torrent-{item_id}",
            free_type=free_type,
            free_end_at=free_end_at,
            push_status=push_status,
        )
        session.add(t)
        await session.flush()
        return t.id


async def _add_site() -> int:
    from app.db.models import Site

    async with session_scope() as session:
        s = Site(name="s1", domain="s1.test", adapter="hdsky")
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


# ------------------------------------------------------------- 选种白名单

async def test_permanent_free_is_selected(db: None) -> None:
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(free_type=FreeType.PERMANENT, site_id=site_id)

    result = await _service().run()
    assert result.selected == 1


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


# ------------------------------------------------- 时魔封顶档排序（FR-1.6 增补）


async def _add_site_with_bonus(
    name: str,
    *,
    capped: bool | None,
    progress: float | None = None,
) -> int:
    """建站 + 写时魔快照（capped=None 表示**没有** site_user_data 行）。"""
    from app.db.models import Site, SiteUserData

    async with session_scope() as session:
        s = Site(name=name, domain=f"{name}.test", adapter="hdsky")
        session.add(s)
        await session.flush()
        if capped is not None:
            session.add(
                SiteUserData(
                    site_id=s.id,
                    hourly_bonus=100.0,
                    hourly_bonus_unit="魔力值",
                    hourly_bonus_capped=capped,
                    hourly_bonus_progress=progress,
                )
            )
        await session.flush()
        return s.id


def _set_order_mode(mode: int) -> None:
    """改运行时参数「同档内排序」（0=按进度升序 1=随机）。"""
    from app.services.runtime_params import get_runtime_params

    get_runtime_params().set_memory("push_within_bucket_order", mode)


async def _pushed_index_ids() -> list[int]:
    """按推送顺序返回被推的 index_id（task_queue 行 id 递增 = 入队顺序）。"""
    from app.db.models import TaskQueue

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(TaskQueue).order_by(TaskQueue.id.asc())
            )
        ).scalars().all()
        return [int(r.payload["index_id"]) for r in rows]


async def test_open_bonus_sites_are_pushed_before_capped_ones(db: None) -> None:
    """时魔未封顶的站优先被推（主排序：未封顶 → 无信号 → 已封顶）。"""
    await _add_downloader()
    capped_site = await _add_site_with_bonus("capped_site", capped=True, progress=110.0)
    open_site = await _add_site_with_bonus("open_site", capped=False, progress=10.0)
    capped_idx = await _add_torrent(site_id=capped_site, item_id="c1")
    open_idx = await _add_torrent(site_id=open_site, item_id="o1")

    result = await _service().run()
    assert result.selected == 2
    assert await _pushed_index_ids() == [open_idx, capped_idx]


async def test_unknown_bonus_site_sits_between_open_and_capped(db: None) -> None:
    """无上限信号的站排中间（未封顶 → 无信号 → 已封顶）。"""
    await _add_downloader()
    open_site = await _add_site_with_bonus("open_site", capped=False, progress=10.0)
    # capped=None → **不建** site_user_data 行（模拟没抓到时魔）
    unknown_site = await _add_site_with_bonus("unknown_site", capped=None)
    capped_site = await _add_site_with_bonus("capped_site", capped=True, progress=110.0)

    open_idx = await _add_torrent(site_id=open_site, item_id="o1")
    unknown_idx = await _add_torrent(site_id=unknown_site, item_id="u1")
    capped_idx = await _add_torrent(site_id=capped_site, item_id="c1")

    result = await _service().run()
    assert result.selected == 3
    assert await _pushed_index_ids() == [open_idx, unknown_idx, capped_idx]


async def test_within_bucket_orders_sites_by_progress_ascending(db: None) -> None:
    """同档内（都未封顶）按各站时魔进度升序——离上限最远的站先。"""
    await _add_downloader()
    # last_seen_at 越晚排越前（默认行为），这里故意让「进度高」的站
    # last_seen_at 更晚，验证是 progress 而不是 last_seen_at 在决定顺序
    far = await _add_site_with_bonus("far_site", capped=False, progress=5.0)
    near = await _add_site_with_bonus("near_site", capped=False, progress=95.0)
    far_idx = await _add_torrent(site_id=far, item_id="f1")
    near_idx = await _add_torrent(site_id=near, item_id="n1")

    result = await _service().run()
    assert result.selected == 2
    assert await _pushed_index_ids() == [far_idx, near_idx]


async def test_capped_site_is_not_pushed_when_budget_is_tight(db: None) -> None:
    """名额不够时，已封顶站被挤出候选（排在最后）。"""
    await _add_downloader()
    capped_site = await _add_site_with_bonus("capped_site", capped=True, progress=110.0)
    open_site = await _add_site_with_bonus("open_site", capped=False, progress=10.0)
    await _add_torrent(site_id=capped_site, item_id="c1")
    await _add_torrent(site_id=open_site, item_id="o1")

    from app.services.runtime_params import get_runtime_params

    get_runtime_params().set_memory("push_max_per_run", 1)
    result = await _service().run()
    assert result.selected == 1
    pushed = await _pushed_index_ids()
    assert len(pushed) == 1


async def test_random_mode_shuffles_within_bucket_but_keeps_bucket_order(
    db: None,
) -> None:
    """随机模式：档内被打乱（含站点顺序），但**档边界不变**。

    档边界是「优先推未封顶站」这个核心行为，随机模式不能破坏它。
    """
    import random as _random

    await _add_downloader()
    open_a = await _add_site_with_bonus("open_a", capped=False, progress=10.0)
    open_b = await _add_site_with_bonus("open_b", capped=False, progress=50.0)
    capped = await _add_site_with_bonus("capped_x", capped=True, progress=110.0)

    # 记录每个种子的 index_id → 所属站
    idx_site: dict[int, int] = {}
    for i in range(3):
        idx_site[await _add_torrent(site_id=open_a, item_id=f"a{i}")] = open_a
        idx_site[await _add_torrent(site_id=open_b, item_id=f"b{i}")] = open_b
    capped_idx = await _add_torrent(site_id=capped, item_id="c1")
    idx_site[capped_idx] = capped

    _set_order_mode(1)
    svc = _service()
    svc._rng = _random.Random(42)  # type: ignore[attr-defined]
    result = await svc.run()
    assert result.selected == 7

    pushed = await _pushed_index_ids()
    # 档边界：已封顶站必须排最后（档边界是「优先推未封顶站」这个核心行为）
    assert pushed[-1] == capped_idx
    # 档内被打乱：未封顶站的 index 不再按站聚在一起
    order_sites = [idx_site[p] for p in pushed[:-1]]
    assert len(set(order_sites)) == 2, "档内应混着两个站"
    assert order_sites != sorted(order_sites), "随机模式应打乱档内站点顺序"


async def test_no_bonus_data_behaves_like_before(db: None) -> None:
    """站点没有时魔数据 → 全部落「无信号」档，行为与改动前一致。"""
    await _add_downloader()
    site_id = await _add_site()
    await _add_torrent(site_id=site_id, item_id="i1")

    result = await _service().run()
    assert result.selected == 1
    assert result.pushed == 0  # 没配下载器时不该推


async def test_bucket_distribution_is_reported_in_steps(db: None) -> None:
    """步骤卡片显示三档站点分布（让用户看到排序依据）。"""
    await _add_downloader()
    await _add_site_with_bonus("s_open", capped=False, progress=10.0)
    await _add_site_with_bonus("s_unknown", capped=None)
    await _add_site_with_bonus("s_capped", capped=True, progress=110.0)

    result = await _service().run()
    steps = {s.name: s for s in result.steps}
    assert "按时魔封顶排序" in steps
    detail = steps["按时魔封顶排序"].detail or ""
    assert "未封顶 1 站" in detail
    assert "无信号 1 站" in detail
    assert "已封顶 1 站" in detail
