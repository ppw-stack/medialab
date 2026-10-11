"""任务队列幂等测试（§6 幂等性 / AC-09）。

核心断言：**同一幂等键重复入队不会产生第二行**，依赖数据库唯一约束。
"""

from __future__ import annotations

import pytest
from app.services.task_queue import (
    TaskKind,
    TaskQueueService,
    make_idem_key,
)


def key(site: int, h: str) -> str:
    return make_idem_key(TaskKind.ADD_TORRENT, site_id=site, info_hash=h)


H = "a" * 40


# ------------------------------------------------------------ 幂等键构造
def test_idem_key_is_case_insensitive_on_hash() -> None:
    assert key(1, H.upper()) == key(1, H)


def test_idem_key_differs_by_site() -> None:
    assert key(1, H) != key(2, H)


def test_idem_key_differs_by_kind() -> None:
    a = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash=H)
    b = make_idem_key(TaskKind.REMOVE_TORRENT, site_id=1, info_hash=H)
    assert a != b


def test_idem_key_handles_missing_site() -> None:
    k = make_idem_key(TaskKind.ADD_TORRENT, site_id=None, info_hash=H)
    assert k.startswith("add_torrent|0|")


def test_idem_key_extra_discriminator() -> None:
    a = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash=H)
    b = make_idem_key(TaskKind.ADD_TORRENT, site_id=1, info_hash=H, extra="retry")
    assert a != b


# ------------------------------------------------------------ 行为
async def test_enqueue_creates_row(db) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(TaskKind.ADD_TORRENT, {"x": 1}, idem_key=key(1, H))
        assert r.created is True


async def test_duplicate_enqueue_is_idempotent(db) -> None:
    """AC-09：重放队列不产生重复任务。"""
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        first = await svc.enqueue(TaskKind.ADD_TORRENT, {"x": 1}, idem_key=key(1, H))
        second = await svc.enqueue(TaskKind.ADD_TORRENT, {"x": 1}, idem_key=key(1, H))
        assert first.created is True
        assert second.created is False
        assert first.task_id == second.task_id


@pytest.mark.parametrize("kind", list(TaskKind))
async def test_all_kinds_have_stable_keys(db, kind: str) -> None:
    from app.db.session import session_scope

    async with session_scope() as s:
        svc = TaskQueueService(s)
        r = await svc.enqueue(kind, {}, idem_key=make_idem_key(kind, site_id=1, info_hash=H))
        assert r.created is True
