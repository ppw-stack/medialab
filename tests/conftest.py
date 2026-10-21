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

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from app.core.config import AppSettings, set_settings
from app.db.bootstrap import db_bootstrap
from app.db.session import dispose_db, reset_db_for_test
from app.services.factory import AdapterFactory
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

    建表走 :meth:`app.db.bootstrap.DbBootstrap.create_all`（与生产同一条路径），
    它还包含 22 条幂等 ALTER 迁移 —— 测试与生产的表结构必须一致，
    否则「测试过了但 prod 崩」这类问题会被漏掉。

    **每个用例前删库文件**：``settings`` 是 session 级（DB 路径固定），
    不删的话上一个用例的行会漏进下一个（``task_queue`` 的 ``idem_key``
    唯一约束会直接让「幂等入队」用例互相干扰）。
    """
    _remove_db_files(app_settings)
    await reset_db_for_test(app_settings)
    await db_bootstrap.create_all()
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


@pytest.fixture
def factory() -> Iterator[AdapterFactory]:
    """初始化全局适配器工厂（服务层测试需要）。

    生产里工厂由 lifespan 的 ``init_factory()`` 建立，是**进程级单例**。
    服务层会调 ``get_factory()``（如 ``downloader_service._resolve_downloader``）
    ——工厂没初始化时会抛 ``RuntimeError``。

    这里建一个工厂并挂到全局单例上，用例结束后还原（不让它跨用例
    泄漏——工厂会缓存适配器实例）。服务层测试要么用假适配器替换方法，
    要么根本不该触发网络。
    """
    import app.services.factory as factory_mod

    prev = factory_mod._factory
    f = factory_mod.init_factory()
    yield f
    factory_mod._factory = prev


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """把退避 sleep 压成 0——测试不该真等 1s/2s/4s。

    只拦 ``app.services.feed_fetcher`` 里的 sleep（退避重试用），
    **不动**其他模块的 sleep。退避「计算」的测试
    （``compute_backoff``）不受影响——它不 sleep。

    背景：拉取路径接回退避重试后，集成测试里的失败用例会各等
    1+2+4 秒，全套从 7s 涨到 24s。

    实现注意：``app.services.feed_fetcher.asyncio`` **就是**全局
    ``asyncio`` 模块（Python 的模块属性），直接
    ``monkeypatch.setattr("app.services.feed_fetcher.asyncio.sleep", ...)``
    等于改全局 ``asyncio.sleep``——**整个测试进程**的 sleep 都会变成 0，
    连用例里自己写的 ``await asyncio.sleep(1.1)`` 也不睡（跨秒断言会失效）。

    故这里直接改 ``feed_fetcher`` 模块里的 ``asyncio`` 引用：
    给它一个**代理模块对象**，只有 ``sleep`` 被换成瞬时版，其余属性
    （``create_task`` / ``wait_for`` / …）照常转发给真 asyncio。
    """
    import types

    import app.services.feed_fetcher as ff

    real_sleep = asyncio.sleep

    async def _instant_sleep(delay: float, *args: object, **kw: object) -> None:
        if delay > 5:
            raise AssertionError(f"退避延迟过大：{delay}s")
        await real_sleep(0, *args, **kw)  # type: ignore[arg-type]

    proxy = types.ModuleType("asyncio")
    proxy.__dict__.update(vars(asyncio))
    proxy.sleep = _instant_sleep  # type: ignore[attr-defined]
    monkeypatch.setattr(ff, "asyncio", proxy)
