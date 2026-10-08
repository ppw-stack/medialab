"""辅种匹配引擎（FR-2.3 核心逻辑）。

对每条 RSS 条目依次执行 7 项判定，任一不通过即跳过并记录原因：

| # | 判定项 | 依据 |
| :-- | :-- | :-- |
| 1 | 已有同 InfoHash | torrents 表（任一下载器，未删除） |
| 2 | 站点去重 | add_history 表（**幂等**，非冷却） |
| 3 | 分类匹配 | 站点 ``category_whitelist`` |
| 4 | 体积上限 | 站点 ``max_size`` |
| 5 | 冷却期 | cooldown_hash（阶梯，见 FR-2.3.1） |
| 6 | 磁盘空间 | 目标分区可用 ≥ size × 1.1 |
| 7 | 路径模板渲染 | 必须生成合法保存路径 |

判定顺序是**性能优化**：1/2 是纯内存/索引查询（最便宜），5/6 涉及 IO，
7 最贵（字符串清洗），7 放在最后可避免为注定被拒的条目做无用功。

**判定与执行分离**：本模块只判定，不添加任务。真正的投递由
:mod:`app.services.seed_engine` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sites.base import FeedItem
from app.core.timeutils import utcnow
from app.db.models import (
    AddHistory,
    Decision,
    FeedItemRecord,
    SkipReason,
    Torrent,
    TorrentStatus,
)
from app.services.cooldown import CooldownService
from app.services.disk import check_space
from app.services.paths import PathRenderError, RenderedPath, render_save_path

#: InfoHash 合法字符（hex），RSS 里混进的脏数据在此拦掉
_HEX = set("0123456789abcdefABCDEF")


class MatchOutcome(StrEnum):
    """判定结果。"""

    ACCEPT = "accept"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class MatchResult:
    """单条条目的判定结果。"""

    outcome: MatchOutcome
    reason: str = ""
    info_hash: str = ""
    save_path: RenderedPath | None = None
    #: 已存在本地的同 hash 任务（outcome=skip 且 reason=duplicate_local 时）
    existing_torrent_id: int | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is MatchOutcome.ACCEPT

    @classmethod
    def skip(cls, reason: str, info_hash: str = "", existing_id: int | None = None) -> MatchResult:
        return cls(
            outcome=MatchOutcome.SKIP,
            reason=reason,
            info_hash=info_hash,
            existing_torrent_id=existing_id,
        )


@dataclass(frozen=True, slots=True)
class SiteMatchPolicy:
    """站点级辅种策略（来自 sites 表）。"""

    site_id: int
    site_name: str
    save_path_template: str
    max_size: int | None = None
    category: str = "cross-seed"
    #: 只辅种分类白名单（FR-2.3 分类匹配）；空集合 = 不限制
    category_whitelist: frozenset[str] = field(default_factory=frozenset)
    tags: frozenset[str] = frozenset()


def normalize_info_hash(raw: str | None) -> str | None:
    """归一化 InfoHash。

    RSS 的 hash 字段不可信：可能是大写、带 ``urn:btih:`` 前缀、含空格，
    甚至被站点当成普通文本。统一转小写 40 位 hex；无法识别返回 ``None``。
    """
    if not raw:
        return None
    text = raw.strip().lower()
    for prefix in ("urn:btih:", "btih:", "sha1:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.replace(" ", "")
    if len(text) == 32:  # base32 编码的 hash
        try:
            import base64

            decoded = base64.b32decode(text.upper())
            if len(decoded) == 20:
                return decoded.hex()
        except (ValueError, TypeError):
            return None
        return None
    if len(text) != 40:
        return None
    if not set(text) <= _HEX:
        return None
    return text


def extract_info_hash(item: FeedItem) -> str | None:
    """从 RSS 条目中提取 InfoHash。

    优先取条目自带字段；缺失时从 magnet 链接解析（NexusPHP 等站点
    常见做法是 ``details.php?id=xxx``，无法直接得到 hash，返回 ``None``）。
    """
    direct = normalize_info_hash(item.info_hash)
    if direct:
        return direct
    for link in (item.torrent_url, item.link):
        if link and "magnet:" in link.lower():
            xt = link.lower().split("xt=urn:btih:", 1)[-1].split("&", 1)[0]
            got = normalize_info_hash(xt)
            if got:
                return got
    return None


class Matcher:
    """FR-2.3 判定器。

    每次 ``match()`` 需要传入当前本地 InfoHash 集合（由调用方预先查好），
    避免每条条目都打一次 DB —— 200 条 RSS × 7 次查询 = 1400 次往返，
    会直接违反非功能·性能（RSS 拉取 200 条 ≤ 30s）。
    """

    def __init__(
        self,
        session: AsyncSession,
        policy: SiteMatchPolicy,
        *,
        local_hashes: frozenset[str] | None = None,
    ) -> None:
        self._session = session
        self._policy = policy
        self._local = local_hashes
        self._cooldown = CooldownService(session)

    async def load_local_hashes(self) -> frozenset[str]:
        """一次性载入本地未删除的 InfoHash 集合（判定项 1）。"""
        rows = (
            await self._session.execute(
                select(Torrent.info_hash).where(Torrent.status != TorrentStatus.DELETED)
            )
        ).scalars().all()
        return frozenset(h.lower() for h in rows)

    async def match(self, item: FeedItem) -> MatchResult:
        """对单条目执行 7 项判定。"""
        info_hash = extract_info_hash(item)
        if info_hash is None:
            return MatchResult.skip(SkipReason.NO_LOCAL_MATCH)

        local = self._local if self._local is not None else await self.load_local_hashes()
        if info_hash in local:
            existing = (
                await self._session.execute(
                    select(Torrent.id).where(Torrent.info_hash == info_hash)
                )
            ).scalar_one_or_none()
            return MatchResult.skip(
                SkipReason.DUPLICATE_LOCAL, info_hash, existing
            )

        # 2) 站点去重（幂等凭证，非冷却）
        seen = (
            await self._session.execute(
                select(AddHistory.id).where(
                    AddHistory.site_id == self._policy.site_id,
                    AddHistory.info_hash == info_hash,
                )
            )
        ).scalar_one_or_none()
        if seen is not None:
            return MatchResult.skip(SkipReason.DUPLICATE_SITE, info_hash)

        # 3) 分类匹配
        if self._policy.category_whitelist:
            cat = (item.category or "").strip()
            if cat not in self._policy.category_whitelist:
                return MatchResult.skip(SkipReason.CATEGORY_MISMATCH, info_hash)

        # 4) 体积上限
        if self._policy.max_size is not None and item.size > self._policy.max_size:
            return MatchResult.skip(SkipReason.SIZE_EXCEEDED, info_hash)

        # 5) 冷却期
        verdict = await self._cooldown.is_blocked(self._policy.site_id, info_hash)
        if verdict.blocked:
            return MatchResult.skip(SkipReason.COOLDOWN, info_hash)

        # 6+7) 路径渲染 → 磁盘校验（顺序有意：渲染失败则不必查磁盘）
        try:
            rendered = render_save_path(
                self._policy.save_path_template,
                site=self._policy.site_name,
                name=item.title or info_hash,
                category=self._policy.category,
                info_hash=info_hash,
            )
        except PathRenderError:
            return MatchResult.skip(SkipReason.PATH_INVALID, info_hash)

        space = check_space(str(rendered.path), item.size)
        if not space.ok:
            return MatchResult.skip(SkipReason.DISK_FULL, info_hash)

        return MatchResult(
            outcome=MatchOutcome.ACCEPT,
            info_hash=info_hash,
            save_path=rendered,
        )

    async def match_many(self, items: list[FeedItem]) -> list[MatchResult]:
        """批量判定。共享一次本地 hash 载入（性能关键）。"""
        if self._local is None:
            self._local = await self.load_local_hashes()
        return [await self.match(item) for item in items]


async def record_decision(
    session: AsyncSession,
    feed_id: int,
    item: FeedItem,
    result: MatchResult,
    *,
    torrent_id: int | None = None,
) -> FeedItemRecord:
    """把判定结果写入 feed_items（FR-2.2「拉取结果写入本地 SQLite」）。

    这是"为什么这个种没被辅种"的唯一排查依据（FR-2.3 每项过滤可追溯）。
    """
    record = FeedItemRecord(
        feed_id=feed_id,
        info_hash=result.info_hash or (item.info_hash or "")[:64],
        title=item.title[:1024],
        link=item.link[:1024],
        size=item.size,
        category=(item.category or None),
        decision=Decision.ACCEPTED if result.accepted else Decision.SKIPPED,
        reject_reason=None if result.accepted else result.reason,
        torrent_id=torrent_id,
        seen_at=utcnow(),
    )
    session.add(record)
    await session.flush()
    return record