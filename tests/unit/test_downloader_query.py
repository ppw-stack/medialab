"""下载器查询测试（Phase 2）。

此前「查默认下载器」的 SQL 散在 3 处（monitor / push_free / push_service），
手抄一遍；另有 ``factory.default_downloader()`` 定义了**宽松**版本
（没配默认时回退第一个 enabled）却零调用。

两套语义的区别很重要，故集中到
:mod:`app.services.downloader_query` 并用参数显式区分：

- **严格**（默认）：只认 ``is_default=True`` 且 enabled，没有就 None
- **宽松**：没配默认时回退到第一个 enabled 的
"""

from __future__ import annotations

from app.db.models import DownloaderInstance
from app.db.session import session_scope
from app.services.downloader_query import (
    get_default_downloader,
    get_downloader_by_id,
)


async def _add_dl(
    name: str,
    *,
    is_default: bool = False,
    enabled: bool = True,
) -> int:
    async with session_scope() as session:
        dl = DownloaderInstance(
            name=name,
            type="qbittorrent",
            url=f"http://{name}:8081",
            is_default=is_default,
            enabled=enabled,
        )
        session.add(dl)
        await session.flush()
        return dl.id


# ------------------------------------------------------------------ 严格

async def test_strict_returns_default(db: None) -> None:
    await _add_dl("dl1")
    dl_id = await _add_dl("dl2", is_default=True)
    got = await get_default_downloader()
    assert got is not None
    assert got.id == dl_id


async def test_strict_returns_none_when_no_default(db: None) -> None:
    """没配默认 → None（**不回退**到 enabled 的）。"""
    await _add_dl("dl1")
    got = await get_default_downloader()
    assert got is None


async def test_strict_skips_disabled_default(db: None) -> None:
    """默认下载器被禁用 → 不返回它。"""
    await _add_dl("dl1", is_default=True, enabled=False)
    got = await get_default_downloader()
    assert got is None


async def test_strict_ignores_non_default_enabled(db: None) -> None:
    """enabled 但不是默认的 → 不返回。"""
    await _add_dl("dl1", enabled=True)
    got = await get_default_downloader()
    assert got is None


# ------------------------------------------------------------------ 宽松

async def test_lenient_falls_back_to_first_enabled(db: None) -> None:
    """宽松：没配默认时回退到第一个 enabled 的。"""
    first = await _add_dl("dl1")
    await _add_dl("dl2")
    got = await get_default_downloader(strict=False)
    assert got is not None
    assert got.id == first


async def test_lenient_prefers_default(db: None) -> None:
    """宽松：配了默认就用默认的。"""
    await _add_dl("dl1")
    default_id = await _add_dl("dl2", is_default=True)
    got = await get_default_downloader(strict=False)
    assert got is not None
    assert got.id == default_id


async def test_lenient_skips_disabled(db: None) -> None:
    """宽松：回退时也只挑 enabled 的。"""
    await _add_dl("dl1", enabled=False)
    second = await _add_dl("dl2")
    got = await get_default_downloader(strict=False)
    assert got is not None
    assert got.id == second


async def test_lenient_none_when_all_disabled(db: None) -> None:
    await _add_dl("dl1", enabled=False)
    got = await get_default_downloader(strict=False)
    assert got is None


# -------------------------------------------------------------- 按 id 查

async def test_get_downloader_by_id(db: None) -> None:
    dl_id = await _add_dl("dl1")
    got = await get_downloader_by_id(dl_id)
    assert got is not None
    assert got.id == dl_id


async def test_get_downloader_by_id_not_found(db: None) -> None:
    assert await get_downloader_by_id(999999) is None


async def test_get_downloader_by_id_ignores_enabled_flag(db: None) -> None:
    """按 id 查不管启用状态——调用方自己判断。"""
    dl_id = await _add_dl("dl1", enabled=False)
    got = await get_downloader_by_id(dl_id)
    assert got is not None
    assert got.enabled is False


# ------------------------------------------------------------ 单一数据源

async def test_no_scattered_default_queries() -> None:
    """「is_default + enabled」的组合查询只应存在于 downloader_query.py。

    防止有人又在新地方手抄一遍。
    """
    from pathlib import Path

    offenders: list[str] = []
    for path in Path("app").rglob("*.py"):
        if path.name == "downloader_query.py":
            continue
        src = path.read_text(encoding="utf-8")
        # 命中「同一个 select 里同时出现两个条件」
        if "is_default.is_(True)" in src and "enabled.is_(True)" in src:
            offenders.append(str(path))
    assert not offenders, f"默认下载器查询散落在：{offenders}"


async def test_factory_has_no_default_downloader_method() -> None:
    """``factory.default_downloader()`` 已删除（零调用 + 语义与实际用法的
    严格版不同）——查询统一走 :mod:`app.services.downloader_query`。
    """
    from app.services.factory import AdapterFactory

    assert not hasattr(AdapterFactory, "default_downloader")
