"""pytest 全局 fixture（Phase 0 安全网）。

设计要点：

- **settings 隔离**：用 ``set_settings()`` 注入全局 override（见
  :func:`app.core.config.set_settings`），让测试用独立 DB / 独立凭据，
  绝不碰 ``/app/data`` 或 ``data-dev/``。
- **DB fixture 必须 function 级**：``aiosqlite`` 连接池绑定创建它的
  event loop，而 ``pytest-asyncio`` 的 loop scope 是 function —— 跨用例
  复用 engine 会抛 ``InterfaceError``。
- **禁止真实网络**：``no_network`` autouse fixture 用 respx 接管全部
  httpx 传输层，未 mock 的请求直接抛错（respx 的
  ``AllMockedAssertionError``）。用例需要发 HTTP 时用 respx 显式 mock。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from app.core.config import AppSettings, set_settings
from app.db.session import dispose_db, reset_db_for_test, writer
from pydantic import SecretStr


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> AppSettings:
    """测试用 settings（session 级：构造本身无副作用）。

    ``scheduler_enabled=False`` 是关键 —— 否则 APScheduler 会在测试里
    真的去拉站点 / 推下载器。
    """
    root: Path = tmp_path_factory.mktemp("seedhelper")
    return AppSettings(
        env="test",
        app_name="SeedHelperTest",
        data_dir=root,
        log_dir=root / "logs",
        db_path=root / "test.db",
        web_username="tester",
        web_password=SecretStr("test-pass-123"),
        web_token=SecretStr("test-token-32-chars-abcdefgh"),
        scheduler_enabled=False,
        log_json=False,
        log_level="WARNING",
    )


@pytest.fixture
def app_settings(settings: AppSettings) -> Iterator[AppSettings]:
    """把测试 settings 注入为全局 override，用例结束后撤销。"""
    set_settings(settings)
    yield settings
    set_settings(None)


@pytest_asyncio.fixture
async def db(app_settings: AppSettings) -> AsyncIterator[None]:
    """重建 DB engine + 建表（function 级 —— 见模块 docstring）。

    建表走 :meth:`SingleWriterQueue.create_all`（与生产同一条路径），
    它还包含 22 条幂等 ALTER 迁移 —— 测试与生产的表结构必须一致，
    否则「测试过了但 prod 崩」这类问题会被漏掉。

    **每个用例前删库文件**：``settings`` 是 session 级（DB 路径固定），
    不删的话上一个用例的行会漏进下一个（``task_queue`` 的 ``idem_key``
    唯一约束会直接让「幂等入队」用例互相干扰）。
    """
    _remove_db_files(app_settings)
    await reset_db_for_test(app_settings)
    await writer.create_all()
    yield
    await dispose_db()


def _remove_db_files(settings: AppSettings) -> None:
    """删掉测试 DB 及其 WAL/SHM 副产物，保证每个用例从空库开始。"""
    db_file = settings.db_file
    for path in (db_file, Path(f"{db_file}-wal"), Path(f"{db_file}-shm")):
        path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def no_network() -> Iterator[respx.MockRouter]:
    """接管全部 httpx 传输层，未 mock 的请求直接失败。

    respx 会 patch ``httpx._client.AsyncClient._transport_for_url``，
    用例内再 ``respx.get(...).mock(...)`` 叠加路由即可（respx 支持嵌套
    router，内层优先匹配）。

    抛出的 ``AllMockedAssertionError`` 明确指出是哪个 URL 没 mock，
    不会静默打真实 PT 站。
    """
    with respx.mock(assert_all_mocked=True) as router:
        yield router
