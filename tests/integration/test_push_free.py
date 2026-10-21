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
from app.core.config import get_settings
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
    return PushFreeService(get_settings(), AdapterFactory())


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
