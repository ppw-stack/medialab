"""pytest 全局夹具。"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

#: 必须在导入 app 之前设置，触发 get_settings() 的 fail-fast 校验
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(
    b"0" * 32
).decode())
os.environ.setdefault("ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def test_key() -> str:
    """确定性测试密钥（仅测试环境使用）。"""
    return base64.urlsafe_b64encode(b"t" * 32).decode()


@pytest.fixture(scope="session")
def settings(tmp_path_factory, test_key: str):
    from app.core.config import AppSettings

    data_dir = tmp_path_factory.mktemp("data")
    return AppSettings(  # type: ignore[call-arg]
        env="test",
        encryption_key=test_key,
        data_dir=data_dir,
        log_dir=data_dir / "logs",
        db_path=data_dir / "test.db",
    )


@pytest_asyncio.fixture
async def db(settings, tmp_path):
    """**每个用例独立**的数据库文件。

    必须 function-scoped 且路径唯一：共享库会让用例之间互相污染
    （例如多个用例都插入 name 相同的 Site，撞唯一约束）。
    """
    from app.core.config import AppSettings
    from app.db.session import dispose_db, init_db, writer

    local = AppSettings(
        env="test",
        encryption_key=settings.encryption_key.get_secret_value(),
        data_dir=tmp_path,
        log_dir=tmp_path / "logs",
        db_path=tmp_path / "test.db",
    )
    init_db(local)
    await writer.create_all()
    try:
        yield writer
    finally:
        await dispose_db()


@pytest.fixture
def sample_site():
    from app.adapters.sites.base import SiteFeature, SiteProfile

    return SiteProfile(
        name="test-pt",
        domain="test-pt.example",
        rss_time_pattern=(
            r"(?:剩余|剩)\s*(?:(?P<d>\d+)\s*天\s*)?"
            r"(?:(?P<h>\d+)\s*小时\s*)?(?:(?P<m>\d+)\s*分)?"
        ),
        window_field_map={"end": "free_until", "alt": ["promotion_end"]},
        features=frozenset({SiteFeature.RSS, SiteFeature.CLOCK_API}),
    )


@pytest.fixture
def random_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


__all__ = ["AsyncIterator", "Path"]