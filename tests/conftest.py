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

**合并说明（strmmanger 并入）**：

两侧原本各有 ``conftest.py``，``settings`` fixture **同名但语义不同**
（辅种域是 session 级、指向固定临时目录；媒体库域是 function 级、每用例
一份）。合并后**统一用辅种域的 session 级版本**（下方 ``settings``）：

- 媒体库域用例依赖的 ``signing_secret`` / ``public_base_url`` 已并入
  同一个 settings 对象；
- 媒体库域原本的 ``db_engine`` / ``db_session`` fixture 保留为**别名**，
  指向辅种域的 ``db`` fixture（同一套 engine，只是返回形式不同），
  故 strm 的测试代码**零改动**即可运行。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import respx
from app.adapters.drivers.fake import FakeDriver
from app.core.config import AppSettings, set_settings
from app.db.bootstrap import db_bootstrap
from app.db.session import dispose_db, reset_db_for_test
from app.services.driver_factory import register_driver
from app.services.factory import AdapterFactory
from fastapi import FastAPI
from pydantic import SecretStr

#: 测试用签名密钥（媒体库域的 STRM 中转签名）
TEST_SIGNING_SECRET = "test-signing-secret"


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> AppSettings:
    """测试用 settings（session 级：构造本身无副作用）。

    ``scheduler_enabled=False`` 是关键 —— 否则 APScheduler 会在测试里
    真的去拉站点 / 推下载器。
    """
    root: Path = tmp_path_factory.mktemp("medialab")
    return AppSettings(
        env="test",
        app_name="MediaLabTest",
        data_dir=root,
        log_dir=root / "logs",
        db_path=root / "test.db",
        web_username="tester",
        web_password=SecretStr("test-pass-123"),
        web_token=SecretStr("test-token-32-chars-abcdefgh"),
        scheduler_enabled=False,
        log_json=False,
        log_level="WARNING",
        # -- 媒体库域 --
        signing_secret=SecretStr(TEST_SIGNING_SECRET),
        public_base_url="http://localhost:8080",
    )


@pytest.fixture
def app_settings(settings: AppSettings) -> Iterator[AppSettings]:
    """把测试 settings 注入为全局 override，用例结束后撤销。

    **注入深拷贝而不是 session 级原对象**：多个用例就地改过
    ``dav_rate_limit_per_min`` / ``dav_enabled`` / ``web_password`` 等字段，
    改在共享对象上会泄漏到后续用例 → 随机 403/429 的顺序依赖。
    每个用例拿到自己的副本，改动随用例结束消失。
    """
    isolated = settings.model_copy(deep=True)
    set_settings(isolated)
    yield isolated
    set_settings(None)


@pytest.fixture
def settings_copy(settings: AppSettings) -> AppSettings:
    """settings 的**独立副本**——给会**就地改**配置的服务用。

    ⚠️ ``settings`` 是 **session 级**对象，被所有用例共享。会改配置的
    服务（如 :func:`app.services.rotation.rotate_secret` 轮换签名密钥时
    会**就地**写 ``settings.signing_secret``）若直接用共享对象，改动会
    **泄漏到后续所有用例**——表现为后面随机冒出的 403「签名无效」。

    故这类用例必须用本 fixture 拿副本，改动随用例结束而消失。
    """
    return settings.model_copy(deep=True)


@pytest_asyncio.fixture
async def db_engine(app_settings: AppSettings) -> AsyncIterator[object]:
    """重建 DB engine + 建表，产出 ``(engine, session_factory)``。

    **function 级** —— 见模块 docstring（aiosqlite 连接池绑定创建它的
    event loop，跨用例复用会抛 ``InterfaceError``）。

    这是**唯一**真正重置 engine 的 fixture。辅种域的 :func:`db` 与
    媒体库域的 :func:`db_engine` / :func:`db_session` 都建立在它之上
    ——**只建一次**，避免两个 fixture 交替时把对方的 engine 换掉
    （那会让先跑的用例拿到已废弃的 engine，报出难懂的 DB 错误）。
    """
    _remove_db_files(app_settings)
    await reset_db_for_test(app_settings)
    await db_bootstrap.create_all()
    from app.db.session import get_engine, get_session_factory

    yield get_engine(), get_session_factory()
    await dispose_db()


@pytest_asyncio.fixture
async def db(db_engine) -> AsyncIterator[None]:
    """辅种域的 DB fixture（**基于** :func:`db_engine`，不重复建库）。

    建表走 :meth:`app.db.bootstrap.DbBootstrap.create_all`（与生产同一条路径），
    它还包含全部幂等 ALTER 迁移 —— 测试与生产的表结构必须一致，
    否则「测试过了但 prod 崩」这类问题会被漏掉。

    **每个用例前删库文件**：``settings`` 是 session 级（DB 路径固定），
    不删的话上一个用例的行会漏进下一个（``task_queue`` 的 ``idem_key``
    唯一约束会直接让「幂等入队」用例互相干扰）。
    """
    yield


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncIterator[object]:
    """媒体库域测试用的独立 DB session。"""
    _engine, factory = db_engine
    async with factory() as session:
        yield session


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


@pytest.fixture(autouse=True)
def no_rate_limit() -> Iterator[None]:
    """测试里关掉全局限速——否则每个用例都慢 1 秒。

    限速器本身的测试见 ``tests/unit/test_rate_limit.py``（那里显式构造）。
    """
    from app.core.rate_limit import rate_limiter

    original = rate_limiter.default_interval
    rate_limiter.set_interval(0.0)
    yield
    rate_limiter.set_interval(original)


@pytest.fixture(autouse=True)
def reset_dav_rate_limiter() -> Iterator[None]:
    """每个用例前清空 WebDAV 限流器的桶（autouse）。

    **为什么需要**：``dav_rate_limiter`` 是**进程级单例**，按 IP + 按用户
    各记一个令牌桶。不清的话上一个用例消耗的令牌会**留给下一个用例**——
    用例一多就会撞上限流阈值，表现为莫名其妙的 **429**。

    strmmanger 单库时测试文件少、顺序凑巧没触发；合并后用例数翻倍，
    立刻暴露。本 fixture 让每个用例从「零消耗」开始，与用例顺序无关。
    """
    from app.services.dav_ratelimit import dav_rate_limiter

    dav_rate_limiter.reset()
    yield
    dav_rate_limiter.reset()


@pytest.fixture(autouse=True)
def env_settings(app_settings: AppSettings) -> Iterator[None]:
    """注入环境变量配置 manager（autouse）。

    生产里 manager 由 lifespan 的 ``init_env_settings_async`` 建立——
    service 层测试不走 lifespan（``scheduler_enabled=False`` 时 lifespan
    仍会跑，但 service-only 测试连 app 都不建），故这里手动注入。

    用空 dict 初始化 manager：业务侧调 ``get_env_setting(key)`` 时回落到
    ``get_settings()`` 上对应字段——既有测试 monkeypatch settings 对象
    的写法仍生效，无需逐个改。

    用例需要测 DB 持久化路径时，自行调用
    ``set_env_settings(EnvSettingsManager({...}))`` 覆盖即可。
    """
    from app.services.env_settings import EnvSettingsManager, set_env_settings

    set_env_settings(EnvSettingsManager())
    yield
    set_env_settings(None)


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


@pytest.fixture
def fake_driver() -> FakeDriver:
    """假驱动实例，并注册到驱动工厂（type = ``fake``）。

    注册的工厂**返回同一个实例**——否则服务层每次构造驱动都会拿到空驱动，
    测试里 ``add_file`` 加的文件就找不到了。
    """
    driver = FakeDriver()
    register_driver("fake", lambda credential=None, device_type=None: driver)
    return driver


# 上传相关的固件放在 ``tests/unit/upload_fakes.py``——那里也有一个叫
# ``fake_driver`` 的固件，**同名会冲突**，故这里显式导入上传那套并改名。
from tests.unit.upload_fakes import (  # noqa: E402
    FakeDriver as UploadFakeDriver,
)
from tests.unit.upload_fakes import (  # noqa: E402
    FakeUploader as UploadFakeUploader,
)


@pytest.fixture
def upload_driver() -> UploadFakeDriver:
    """上传用的假光鸭驱动（内存目录树）。"""
    return UploadFakeDriver()


@pytest.fixture
def upload_uploader() -> UploadFakeUploader:
    """上传用的假上传器（记录调用，不真传）。"""
    return UploadFakeUploader()


@pytest.fixture
def http_mock():
    """构造带 ``MockTransport`` 的 httpx.AsyncClient（附件下载测试用）。

    用法::

        async with http_mock(b"JPEG") as client:
            ...
    """
    from contextlib import asynccontextmanager

    import httpx

    @asynccontextmanager
    async def _make(
        body: bytes = b"", status: int = 200
    ) -> AsyncIterator[httpx.AsyncClient]:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=body)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            yield client

    return _make


@pytest.fixture
def services(app_settings: AppSettings) -> Iterator[None]:
    """初始化服务单例（直链缓存 + 中转服务）。"""
    from app.services.svc_factory import init_services, reset_services

    reset_services()
    init_services(app_settings)
    yield
    reset_services()


@pytest_asyncio.fixture
async def app_client(
    app_settings: AppSettings, db_engine, fake_driver
) -> AsyncIterator[object]:
    """已初始化 lifespan 依赖的测试客户端（媒体库域专用）。

    挂载**媒体库域**的路由（认证 / 系统 / 中转），用 ASGITransport
    直连应用（不跑完整 lifespan——服务单例由 ``services`` fixture 管）。
    """
    from app.api.auth import router as auth_router
    from app.api.routes import redirect, system
    from app.services.svc_factory import init_services, reset_services
    from httpx import ASGITransport, AsyncClient

    reset_services()
    init_services(app_settings)

    app = FastAPI()
    app.include_router(system.router)
    app.include_router(auth_router)
    app.include_router(redirect.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    reset_services()


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


@pytest.fixture(autouse=True)
def fast_production_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """把生产级「等待常量」压到近零——测试里不该真的等 5s / 3s。

    背景：:func:`fast_backoff` 只拦了 ``feed_fetcher`` 的退避 sleep，但还有
    一批**生产级等待常量**在测试里真睡（浴盆曲线式拖慢全量）：

    - 归拢落盘验证 ``VERIFY_DELAY_SEC = 5s``：慢用例跑 25 轮 → ~125s
    - 分享转存轮询 ``SHARE_TRANSFER_POLL_SEC = 3s``
    - 附件下载退避 ``RETRY_BACKOFF_SEC = 5s``：6 个用例各 ~15s
    - 元数据请求退避 ``tmdb.RETRY_BACKOFF_SEC = 1.5s``

    实测这四处是全量测试的主要瓶颈（单用例最高 52s，合计 >160s）。

    **只 patch 常量本身，不动 ``asyncio.sleep``** —— 这样依赖真实时间的
    测试（``test_rate_limit`` / ``test_test_infra``）完全不受影响。用例内
    若显式 setattr 了这些常量（如归拢超时用例在测试体里设成 0.2 / 1.0），
    测试体的值后执行、会覆盖本 fixture，语义不变。

    ``SHARE_TRANSFER_WAIT_SEC`` 只压到 5s 而非 0：正常用例第一次轮询
    （假驱动同步落地）即命中、不受影响；万一有用例故意不落地，也不会卡满
    生产的 600s。显式设过该值的用例同样会覆盖本 fixture。
    """
    from app.adapters.metadata import tmdb
    from app.services import aggregate_sync, attachments, migration

    monkeypatch.setattr(aggregate_sync, "VERIFY_DELAY_SEC", 0.0)
    monkeypatch.setattr(aggregate_sync, "SHARE_TRANSFER_POLL_SEC", 0.01)
    monkeypatch.setattr(aggregate_sync, "SHARE_TRANSFER_WAIT_SEC", 5.0)
    monkeypatch.setattr(migration, "VERIFY_DELAY_SEC", 0.0)
    monkeypatch.setattr(attachments, "RETRY_BACKOFF_SEC", 0.0)
    monkeypatch.setattr(tmdb, "RETRY_BACKOFF_SEC", 0.0)
