"""下载器查询（共享查询逻辑）。

此前「查默认下载器」的 SQL 散在 3 处（``monitor.py`` / ``push_free.py`` /
``push_service.py``），各处手抄一遍；另有
:meth:`app.services.factory.AdapterFactory.default_downloader` 定义了
**宽松**版本（没配默认时回退到第一个 enabled）却零调用。

两套语义的区别很重要，故集中到本模块并用参数显式区分：

- **严格**（``strict=True``，默认）：只认 ``is_default=True`` 且 enabled。
  没有就返回 ``None``。作业与 API 走这个——用户必须**显式**指定默认
  下载器，静默挑一个 enabled 的可能推到错的下载器。
- **宽松**（``strict=False``）：没配默认时回退到第一个 enabled 的。
  供将来「没有站点→下载器绑定」的业务（如辅种引擎）使用。
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import DownloaderInstance
from app.db.session import session_scope

__all__ = [
    "get_default_downloader",
    "get_downloader_by_id",
]


async def get_default_downloader(*, strict: bool = True) -> DownloaderInstance | None:
    """取默认下载器。

    :param strict: True（默认）= 只认 ``is_default=True`` 且 enabled 的，
        没配就返回 ``None``；False = 没配默认时回退到第一个 enabled 的
        （按 id 升序，保证结果稳定）。
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                select(DownloaderInstance)
                .where(
                    DownloaderInstance.is_default.is_(True),
                    DownloaderInstance.enabled.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
        if strict:
            return None
        row = (
            await session.execute(
                select(DownloaderInstance)
                .where(DownloaderInstance.enabled.is_(True))
                .order_by(DownloaderInstance.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row


async def get_downloader_by_id(downloader_id: int) -> DownloaderInstance | None:
    """按 id 取下载器（不管是否默认 / 是否启用——调用方自己判断）。"""
    async with session_scope() as session:
        return (
            await session.execute(
                select(DownloaderInstance).where(
                    DownloaderInstance.id == downloader_id
                )
            )
        ).scalar_one_or_none()
