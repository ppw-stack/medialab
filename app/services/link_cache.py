"""本地直链缓存（L1 内存 + L2 SQLite）。

**为什么缓存 key 必须带 UA**：115 直链参数 ``f=1`` 表示「请求直链时的
UA 必须与访问直链时的 UA 一致」。按 ``media_id`` 单键缓存会导致换设备 /
换播放器必然 403。UA 分桶是**正确性要求**，不是性能优化。

**TTL 提前失效**::

    expires_at = parse(直链的 ?t=) - margin(默认 300s)

避免把「马上要过期」的 URL 发给客户端。直链 URL 无 ``?t=`` 时用
fallback TTL（默认 600s）。

**单飞**：同 ``(media_id, ua)`` 并发 miss 时只有第一个请求打网盘 API，
其余 await 同一结果——否则并发播放直接触发风控。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.core.timeutils import bj_now
from app.db.models import LinkCacheEntry
from app.db.session import session_scope

logger = get_logger(__name__)

#: 缓存写失败后的重试间隔（秒）。
#:
#: ``database is locked`` 通常是**瞬时**的——别的连接正持写锁，几百毫秒后
#: 就释放。重试一次比直接放弃命中率更高。
_LOCK_RETRY_DELAY = 0.3


@dataclass(frozen=True)
class CachedLink:
    """一条缓存的直链。"""

    url: str
    expires_at: int

    def is_valid(self, now: float | None = None) -> bool:
        """是否仍在有效期内。"""
        current = time.time() if now is None else now
        return self.expires_at > current


def parse_expires_at(
    url: str,
    *,
    default_ttl_sec: int | None = None,
    safety_margin_sec: int | None = None,
) -> int:
    """从直链 URL 的 ``?t=`` 参数提取过期时间戳；无该参数则用 fallback TTL。

    Args:
        url: 直链 URL。
        default_ttl_sec: URL 无 ``?t=`` 时的 fallback TTL 秒数。
        safety_margin_sec: 提前失效秒数（防止发出将过期的 URL）。

    Returns:
        过期时间戳（unix 秒，已扣除安全余量）。
    """
    from app.core.config import get_settings

    settings = get_settings()
    if default_ttl_sec is None:
        default_ttl_sec = settings.link_cache_default_ttl_sec
    if safety_margin_sec is None:
        safety_margin_sec = settings.link_cache_ttl_margin_sec

    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    raw = query.get("t", "")
    if raw.isascii() and raw.isdigit():
        return int(raw) - safety_margin_sec
    return int(time.time()) + default_ttl_sec - safety_margin_sec


class LinkCache:
    """两级直链缓存。

    - L1：进程内 LRU（``OrderedDict``），容量由配置决定
    - L2：SQLite ``link_cache`` 表，跨重启存活

    L2 写入在**事务外**的网络 IO 之后、单独的短事务内完成。
    """

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._l1_capacity = settings.link_cache_l1_size
        self._l1: OrderedDict[tuple[int, str], CachedLink] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._flight = SingleFlight()

    # -- L1 --

    def _l1_get(self, media_id: int, ua_hash: str) -> CachedLink | None:
        key = (media_id, ua_hash)
        entry = self._l1.get(key)
        if entry is None:
            return None
        if not entry.is_valid():
            del self._l1[key]
            return None
        self._l1.move_to_end(key)
        return entry

    def _l1_put(self, media_id: int, ua_hash: str, link: CachedLink) -> None:
        key = (media_id, ua_hash)
        self._l1[key] = link
        self._l1.move_to_end(key)
        while len(self._l1) > self._l1_capacity:
            self._l1.popitem(last=False)

    # -- L2 --

    async def _l2_get(self, media_id: int, ua_hash: str) -> CachedLink | None:
        try:
            async with session_scope() as db:
                row = (
                    await db.execute(
                        select(LinkCacheEntry).where(
                            LinkCacheEntry.media_id == media_id,
                            LinkCacheEntry.ua_hash == ua_hash,
                        )
                    )
                ).scalar_one_or_none()
        except OperationalError as exc:
            # **读缓存失败 ≠ 不能播放**——当作「未命中」继续换取直链。
            #
            # 2026-11-12 prod 事故：扫描作业大批量写库时，中转请求读缓存
            # 撞上 SQLite 写锁（DELETE 模式是**全库排他**锁），异常冒泡 →
            # **播放 500**。直链缓存是优化，不是播放的前提。
            logger.warning("读取直链缓存失败（按未命中处理）：%s", exc)
            return None
        if row is None:
            return None
        link = CachedLink(url=row.url, expires_at=row.expires_at)
        if not link.is_valid():
            return None
        return link

    async def _l2_put(
        self, media_id: int, ua_hash: str, url: str, expires_at: int
    ) -> None:
        now = bj_now()
        for attempt in (1, 2):
            try:
                async with session_scope() as db:
                    existing = (
                        await db.execute(
                            select(LinkCacheEntry).where(
                                LinkCacheEntry.media_id == media_id,
                                LinkCacheEntry.ua_hash == ua_hash,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        db.add(
                            LinkCacheEntry(
                                media_id=media_id,
                                ua_hash=ua_hash,
                                url=url,
                                expires_at=expires_at,
                                created_at=now,
                            )
                        )
                    else:
                        existing.url = url
                        existing.expires_at = expires_at
                return
            except OperationalError as exc:
                # 写锁冲突是**瞬时**的（别的连接正持写锁）——退避重试一次。
                if attempt == 1:
                    await asyncio.sleep(_LOCK_RETRY_DELAY)
                    continue
                # 仍失败就**放弃写缓存**：直链已经换到手，播放不能因为
                # 「缓存写不进去」而失败（2026-11-12 prod 事故：这里抛出去
                # 直接变成播放 500）。
                logger.warning("写入直链缓存失败（忽略，不影响播放）：%s", exc)
                return

    # -- 对外 --

    async def get(self, media_id: int, ua_hash: str) -> CachedLink | None:
        """按 (media_id, ua) 查缓存。L1 未命中则查 L2 并回填 L1。"""
        link = self._l1_get(media_id, ua_hash)
        if link is not None:
            self._hits += 1
            return link
        link = await self._l2_get(media_id, ua_hash)
        if link is not None:
            self._l1_put(media_id, ua_hash, link)
            self._hits += 1
            return link
        self._misses += 1
        return None

    async def put(self, media_id: int, ua_hash: str, url: str, expires_at: int) -> None:
        """写入缓存（L1 + L2）。

        **L2 写失败不影响调用方**（降级成「只有 L1 内存缓存」）——缓存是
        优化，直链已经换到手，不能因为「缓存写不进去」让播放失败。
        """
        link = CachedLink(url=url, expires_at=expires_at)
        self._l1_put(media_id, ua_hash, link)
        await self._l2_put(media_id, ua_hash, url, expires_at)

    async def get_or_fetch(
        self,
        media_id: int,
        ua_hash: str,
        fetch: Callable[[], Awaitable[tuple[str, int]]],
    ) -> CachedLink:
        """查缓存；miss 时经**单飞**调用 ``fetch`` 并写回。

        **缓存查找必须放在单飞内部**——若放在外面，会出现这个竞态：

        1. 请求 A 查缓存（miss），进入单飞，开始 fetch
        2. 请求 B 也查缓存（miss，但 B 的查询更慢，尚未进入单飞）
        3. A 完成 fetch 并写缓存，单飞槽位因引用计数归零被回收
        4. B 终于进入单飞 → **新建槽位** → 又 fetch 一次

        结果：并发请求打了两次网盘 API——正是单飞要防的事。
        把查找放进单飞内部后，第 4 步 B 进入单飞时缓存已命中，直接返回，
        不会重复 fetch。

        Args:
            media_id: 媒体条目 ID。
            ua_hash: UA 哈希（缓存分桶键）。
            fetch: 无参异步可调用，返回 ``(url, expires_at)``。

        Returns:
            有效的直链。
        """
        # 快路径：L1 命中。**同步**检查（无 await）→ 无交错点，无竞态。
        fast = self._l1_get(media_id, ua_hash)
        if fast is not None:
            self._hits += 1
            return fast

        key = f"{media_id}:{ua_hash}"

        async def _do_get_or_fetch() -> CachedLink:
            # 缓存查找在单飞内部——见上面的竞态说明。
            cached = await self.get(media_id, ua_hash)
            if cached is not None:
                return cached
            url, expires_at = await fetch()
            await self.put(media_id, ua_hash, url, expires_at)
            return CachedLink(url=url, expires_at=expires_at)

        return await self._flight.run(key, _do_get_or_fetch)

    async def cleanup_expired(self) -> int:
        """删除 L2 中已过期的条目，返回删除行数。"""
        now = int(time.time())
        async with session_scope() as db:
            result = await db.execute(
                delete(LinkCacheEntry).where(LinkCacheEntry.expires_at <= now)
            )
            deleted = int(getattr(result, "rowcount", 0) or 0)
        # 同步清理 L1 的过期项
        stale = [key for key, entry in self._l1.items() if not entry.is_valid()]
        for key in stale:
            del self._l1[key]
        if deleted:
            logger.info("清理过期直链缓存 %d 条", deleted)
        return deleted

    def stats(self) -> dict[str, int | float]:
        """缓存统计（监控面板用）。"""
        total = self._hits + self._misses
        return {
            "l1_size": len(self._l1),
            "l1_capacity": self._l1_capacity,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
            "in_flight": self._flight.in_flight,
        }
