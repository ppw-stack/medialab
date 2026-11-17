"""单实例租约测试（多 worker / 多副本守卫）。"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio
from app.core.config import set_settings
from app.db.session import session_scope
from app.services.instance_lease import (
    InstanceLeaseError,
    acquire_instance_lease,
)
from sqlalchemy import text


@pytest_asyncio.fixture
async def prod_like_settings(app_settings, settings_copy):
    """把全局 settings 临时切成非 test 环境，让守卫真正生效。"""
    prod = settings_copy.model_copy(update={"env": "dev"})
    set_settings(prod)
    yield prod
    set_settings(app_settings)


@pytest.mark.asyncio
async def test_acquire_and_release(db_engine, prod_like_settings) -> None:
    """抢到 → 释放 → 再抢成功。"""
    first = await acquire_instance_lease()
    assert first is not None
    assert first.instance_id

    await first.close()
    second = await acquire_instance_lease()
    assert second is not None
    assert second.instance_id != first.instance_id
    await second.close()


@pytest.mark.asyncio
async def test_second_instance_is_rejected(db_engine, prod_like_settings) -> None:
    """租约有效期内第二个实例必须被拒绝（fail-fast）。"""
    first = await acquire_instance_lease()
    assert first is not None
    try:
        with pytest.raises(InstanceLeaseError, match=r"单实例|另一个正在运行"):
            await acquire_instance_lease()
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_expired_lease_can_be_taken_over(db_engine, prod_like_settings) -> None:
    """进程崩溃（租约过期）后，新实例应能接管。"""
    first = await acquire_instance_lease()
    assert first is not None
    async with session_scope() as db:
        await db.execute(
            text("UPDATE app_instance_lease SET expires_at = :old WHERE id = 1"),
            {"old": time.time() - 1},
        )
    try:
        second = await acquire_instance_lease()
        assert second is not None
        assert second.instance_id != first.instance_id
        await second.close()
    finally:
        # first 的心跳可能在 close 前把过期租约续回来；这里只清心跳。
        await first.close()


@pytest.mark.asyncio
async def test_test_env_skips_lease(db_engine, app_settings) -> None:
    """测试环境跳过租约——测试固件会在同一 DB 上多次 create_app。"""
    assert app_settings.env == "test"
    assert await acquire_instance_lease() is None
