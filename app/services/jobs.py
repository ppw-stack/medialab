"""站点拉取作业（按站遍历拉取种子列表 → torrent_index）。

- :func:`fetch_all_sites` — 按站遍历所有启用站点，调用各 adapter 的
  :meth:`SiteAdapter.search_torrents` 拉取最新种子列表，把 :class:`FeedItem`
  upsert 到 :class:`TorrentIndex` 表。

作业**注册**在 :mod:`app.services.job_registry`（本模块只管业务逻辑）。

注：本模块不再做辅种匹配或限免过滤——全部站点拉取的种子都写入索引表
供查询/筛选，外部系统按需使用。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.adapters.sites.base import (
    BreakerTripped,
    FeedItem,
    Promotion,
    SiteAdapter,
    SiteFetchError,
    SitePermanentError,
)
from app.core.logging import get_logger
from app.core.metrics import feed_pulls, site_breaker_open, stage_duration
from app.core.metrics import state as metric_state
from app.core.timeutils import bj_now, bj_now_dt, format_bj, parse_bj
from app.db.models import Site, TorrentIndex
from app.db.session import session_scope
from app.domain.enums import FreeType
from app.services.factory import AdapterFactory
from app.services.feed_fetcher import FeedFetcher, PullStatus
from app.services.scheduler import JobStep, JobStepItem, build_items
from app.services.window_parser import WindowParser

logger = get_logger(__name__)

#: 429 限流 → 熔断时长（分钟）。429 明确是限流，冷却 1 小时。
_BREAKER_MINUTES_429 = 60
#: 403 → 熔断时长（分钟）。403 可能是 cookie 失效而非限流，
#: 熔断 1 小时会让「改完 cookie 仍被跳过」，故减半。
_BREAKER_MINUTES_403 = 30

#: 「优惠复查」的并发数。优惠历史是**逐种子**请求（``promotion.php``），
#: 并发太高会被站点限流；5 路 × 单条约 0.25s，够用且不压站点。
_PROMO_LOOKUP_CONCURRENCY = 5
#: 单站单轮复查上限。首轮上线时库里几百个条目全是「新增」→ 分几轮补齐
#: （没查到的下轮仍算新增，自然续查）。
_PROMO_LOOKUP_MAX = 200
#: 已确认「当前非免费」的条目复查间隔。
#:
#: 不能对 ``unknown`` 每轮都查——HDWinG 一轮 300 条，每 10 分钟一次就是
#: 4.3 万请求/天。免费窗口通常 ≥6 小时，1 小时复查一次足以发现新开的
#: 促销；当前新条目速率约 4–18 条/小时，稳态请求量远低于全量。
_PROMO_UNKNOWN_RECHECK_MINUTES = 60
#: 非免费判定在 ``free_payload`` 里的标记键。
_PROMO_CHECKED_AT_KEY = "promotion_checked_at"

async def fetch_all_sites(factory: AdapterFactory) -> dict[str, object]:
    """按站遍历拉取种子列表，写入 :class:`TorrentIndex`。

    Returns: ``{"total_written": N, "scanned_sites": M, "steps": [...]}``。
    失败站记 ERROR 但不阻塞其他站。

    事务边界：网络拉取在**写事务外**进行，upsert 每站一个短事务——
    原先一个 session 包住全部站点，首个 upsert 即开写锁并横跨后续站点的
    网络 IO，与 push_free 等写作业并发时频繁 ``database is locked``。

    熔断（FR-2.2）：站点返回 403/429 时抛 :class:`BreakerTripped`，
    本站被熔断（写入 ``sites.breaker_until``），后续轮次**跳过该站
    且不发请求**。熔断是保护性跳过，计入 ``skipped_breaker``
    而**不是** ``failed_sites``——混进失败会让告警逻辑误报。
    """
    started = asyncio.get_running_loop().time()
    total_written = 0
    # (site_id, name, pulled, ok, ko, site_error)
    summary: list[tuple[int, str, int, int, int, str | None]] = []
    # 站点级失败的站（拉取异常 / 优惠判定永久失败）——与"站点没有种子"区分开
    site_failures: list[tuple[str, str]] = []
    # 被熔断跳过的站（不发请求）
    breaker_skipped: list[tuple[str, str]] = []
    # 逐站明细（前端「步骤卡片」展开后可见）——回答"是哪个站出了问题"
    site_items: list[JobStepItem] = []

    async with session_scope() as session:
        stmt = select(Site).where(Site.enabled.is_(True))
        result = await session.execute(stmt)
        rows: list[Site] = list(result.scalars().all())

    for row in rows:
        # 熔断期内直接跳过——继续请求只会加速账号暴露
        if _is_breaker_open(row.breaker_until):
            breaker_skipped.append((row.name, row.breaker_until or ""))
            site_items.append(
                JobStepItem(
                    row.name, "skip", None,
                    f"熔断期（至 {row.breaker_until}）",
                )
            )
            logger.info(
                "站点 %s 处于熔断期（至 %s），跳过本轮拉取",
                row.name, row.breaker_until,
            )
            continue

        adapter: SiteAdapter | None = None
        # 优惠判定的**站点级**永久失败（凭据失效）——非 None 即站点失败
        promo_error: str | None = None
        try:
            adapter = factory.site(row)
        except Exception as exc:
            logger.warning("站点 %s 适配器创建失败：%s", row.name, exc)
            feed_items: list[FeedItem] = []
            pull_error: str | None = f"适配器创建失败：{exc}"
            breaker_until: str | None = None
        else:
            feed_items, pull_error, breaker_until = await _pull_site_items(
                adapter, row
            )
        if (
            adapter is not None
            and feed_items
            and pull_error is None
            and breaker_until is None
        ):
            # 免费判定（仅 hdwing 这类「优惠标记是 AJAX 占位符」的站）：
            # 增量复查新增 / 已过期的条目，其余沿用旧判定。
            promo = await _resolve_promotions(adapter, row, feed_items)
            feed_items = promo.items
            if promo.breaker is not None:
                breaker_until = await _open_breaker(row, promo.breaker)
                logger.warning(
                    "站点 %s 优惠判定触发熔断，暂停拉取至 %s：%s",
                    row.name,
                    breaker_until,
                    promo.breaker,
                )
            if promo.error is not None:
                # 凭据失效等**永久**故障 → 站点级失败。
                # 不能当单条失败吞掉：那会把故障静默写成「非免费」。
                promo_error = f"优惠判定失败：{promo.error}"
        ok, ko = await _upsert_site_items(row, feed_items)
        total_written += ok
        # 站点级失败 = 拉取失败 **或** 优惠判定永久失败（凭据失效）
        site_error = pull_error or promo_error
        summary.append((row.id, row.name, ok + ko, ok, ko, site_error))
        if site_error is not None:
            site_failures.append((row.name, site_error))
            site_items.append(
                JobStepItem(row.name, "fail", ok + ko, site_error)
            )
        elif breaker_until is not None:
            # 本轮触发熔断的站也计入「熔断跳过」——它本轮没拉到数据，
            # 但性质是保护性熔断而非故障，不该进 failed_sites。
            breaker_skipped.append((row.name, breaker_until))
            site_items.append(
                JobStepItem(
                    row.name, "warn", ok + ko,
                    f"触发熔断（至 {breaker_until}）",
                )
            )
        else:
            site_items.append(
                JobStepItem(row.name, "ok", ok + ko, f"写入 {ok} 条种子")
            )

    # 失败站 = 拉取失败（网络/DNS/适配器异常）、优惠判定永久失败
    # （凭据失效）或写入失败（DB 错误）的站。
    # 按站名去重——同一站可能两类错误都命中。
    failed_site_names: set[str] = {
        name for _, name, _, _, ko, _ in summary if ko > 0
    }
    failed_site_names |= {name for name, _ in site_failures}
    total_failed = len(failed_site_names)
    total_breaker_skipped = len(breaker_skipped)

    open_breaker_names = {name for name, _ in breaker_skipped}
    for row in rows:
        site_breaker_open.labels(site=row.name).set(
            1 if row.name in open_breaker_names else 0
        )
    metric_state["sites_breaker_open"].set(len(open_breaker_names))

    if total_written:
        logger.info(
            "站点拉取：共 %d 条种子写入 torrent_index（站数 %d）",
            total_written, len(rows),
        )
    else:
        logger.info("站点拉取：无种子写入（站数 %d）", len(rows))
    if site_failures:
        logger.warning(
            "站点失败 %d 站：%s",
            len(site_failures),
            ", ".join(f"{name}({err[:60]})" for name, err in site_failures),
        )
    if breaker_skipped:
        logger.warning(
            "站点熔断跳过 %d 站：%s",
            total_breaker_skipped,
            ", ".join(name for name, _ in breaker_skipped),
        )

    stage_duration.labels(stage="fetch_all_sites").observe(
        asyncio.get_running_loop().time() - started
    )
    failed_names = ", ".join(sorted(failed_site_names))
    breaker_names = ", ".join(sorted(name for name, _ in breaker_skipped))
    site_items, site_items_total = build_items(site_items)
    return {
        "total_written": total_written,
        "scanned_sites": len(rows),
        "failed_sites": total_failed,
        # 熔断跳过（保护性跳过，不算故障）
        "skipped_breaker": total_breaker_skipped,
        "summary": f"拉取 {len(rows)} 站，写入 {total_written} 条种子"
        + (f"，{total_failed} 站失败（{failed_names}）" if total_failed else "")
        + (
            f"，{total_breaker_skipped} 站熔断跳过（{breaker_names}）"
            if total_breaker_skipped
            else ""
        ),
        "steps": [
            JobStep(
                "查询启用站点", "ok", len(rows), f"{len(rows)} 站"
            ),
            JobStep(
                "拉取种子列表",
                "warn" if total_failed else "ok",
                len(rows) - total_failed - total_breaker_skipped,
                f"逐站拉取（上限 3 页），{total_failed} 站失败"
                + (f"：{failed_names}" if total_failed else "")
                + (
                    f"，{total_breaker_skipped} 站熔断跳过"
                    if total_breaker_skipped
                    else ""
                )
                if (total_failed or total_breaker_skipped)
                else "逐站拉取（上限 3 页）",
                items=site_items,
                items_total=site_items_total,
            ),
            JobStep(
                "写入 torrent_index",
                "ok" if total_written else "skip",
                total_written,
                f"{total_written} 条种子 upsert",
            ),
        ],
    }


async def _pull_site_items(
    adapter: SiteAdapter, row: Site
) -> tuple[list[FeedItem], str | None, str | None]:
    """拉取单站种子列表（网络 IO，不持写事务）。

    Returns: ``(items, error, breaker_until)``：

    - ``error`` 非 None → **拉取失败**（网络/DNS/解析/适配器异常）
    - ``breaker_until`` 非 None → 本轮触发**熔断**（403/429 限流），
      已写入 ``sites.breaker_until``，本轮不再请求该站
    - 两者都为 None → 正常（items 可能为空 = 站点确实没种子）

    区分"拉取失败"与"站点确实没有种子"很重要：两者都返回空列表，
    但前者是故障（站点被墙 / cookie 失效 / DNS 挂了），若混为一谈，
    作业统计会报"全部成功"而实际漏掉了故障站（本次 prod 的 hddolby
    DNS 解析失败就是这样被掩盖的）。

    熔断（FR-2.2）：适配器抛 :class:`BreakerTripped`（403/429 限流）时
    写 ``sites.breaker_until`` 并**立即放弃**该站——继续请求只会加速
    账号暴露。此时本站算「熔断跳过」而非「拉取失败」。
    """
    # 分页拉取 + 指数退避重试（FR-2.2）。
    # 重试让 DNS 抖动 / 连接重置这类**瞬态**故障有机会自愈，
    # 而不必每次都记成失败站。
    fetcher = FeedFetcher(row.name)
    result = await fetcher.fetch_pages(adapter, max_pages=3, page_size=200)
    feed_items = list(result.items)

    if result.status == PullStatus.BREAKER_TRIPPED:
        # 限流/封禁 → 熔断本站，本轮不再请求
        feed_pulls.labels(result="breaker", site=row.name).inc()
        breaker_until = await _open_breaker(row, result.error)
        logger.warning(
            "站点 %s 触发熔断，暂停拉取至 %s：%s",
            row.name, breaker_until, result.error,
        )
        return (feed_items, None, breaker_until)  # 熔断不算「拉取失败」
    if result.status == PullStatus.PARSE_ERROR:
        feed_pulls.labels(result="error", site=row.name).inc()
        logger.warning("站点 %s 解析失败：%s", row.name, result.error)
        return (feed_items, result.error, None)
    if result.status == PullStatus.HTTP_ERROR:
        feed_pulls.labels(result="error", site=row.name).inc()
        logger.warning("站点 %s 拉取失败：%s", row.name, result.error)
        # 保留已拉到的分页数据：第 3 页失败不该让前 2 页白拉
        # （但本站仍记为失败站，交由上层统计）
        return (feed_items, result.error, None)

    if feed_items:
        feed_pulls.labels(result="ok", site=row.name).inc(len(feed_items))
    return (feed_items, None, None)


def _is_breaker_open(breaker_until: str | None) -> bool:
    """熔断是否仍然生效（``breaker_until`` 晚于当前时间）。"""
    if not breaker_until:
        return False
    until = parse_bj(breaker_until)
    if until is None:
        return False
    return until > bj_now_dt()


async def _open_breaker(row: Site, reason: str) -> str:
    """写入熔断截止时刻，返回写入的时间字符串。

    403 与 429 熔断时长不同：403 可能是 cookie 失效（不是限流），
    熔断 1 小时会让"改 cookie 后仍被跳过"；429 明确是限流，熔断 1 小时。
    """
    minutes = _BREAKER_MINUTES_403 if "403" in reason else _BREAKER_MINUTES_429
    until = format_bj(bj_now_dt() + timedelta(minutes=minutes))
    async with session_scope() as session:
        await session.execute(
            update(Site).where(Site.id == row.id).values(breaker_until=until)
        )
    return until


# ---------------------------------------------------------------- 优惠（免费判定）

async def _known_free_states(
    site_id: int,
) -> dict[str, tuple[str | None, str | None, dict[str, object]]]:
    """库里已有的免费判定：``{item_id: (free_type, free_end_at, free_payload)}``。"""
    async with session_scope() as session:
        rows = await session.execute(
            select(
                TorrentIndex.item_id,
                TorrentIndex.free_type,
                TorrentIndex.free_end_at,
                TorrentIndex.free_payload,
            ).where(TorrentIndex.site_id == site_id)
        )
        return {
            (item_id or ""): (
                free_type,
                free_end_at,
                payload if isinstance(payload, dict) else {},
            )
            for item_id, free_type, free_end_at, payload in rows.all()
        }


def _needs_recheck(
    state: tuple[str | None, str | None, dict[str, object]],
    now: datetime,
) -> bool:
    """旧判定是否需要重新查站点。

    - ``PERMANENT`` → 不再复查
    - ``TIMED`` 且窗口已过 → 复查（可能不再免费，或又开了新窗口）
    - ``TIMED`` 且窗口未过 → 不复查
    - 其它（含 ``UNKNOWN``）→ 有「已查非免费」标记且未到 TTL 则不复查；
      没有标记（历史数据 / 新条目）→ 复查
    """
    free_type, free_end_at, payload = state
    if free_type == FreeType.PERMANENT:
        return False
    if free_type == FreeType.TIMED:
        end = parse_bj(free_end_at)
        return end is None or end <= now

    checked_raw = payload.get(_PROMO_CHECKED_AT_KEY)
    checked = parse_bj(str(checked_raw)) if checked_raw else None
    if checked is None:
        return True
    return now - checked >= timedelta(minutes=_PROMO_UNKNOWN_RECHECK_MINUTES)


def _payload_from_state(
    state: tuple[str | None, str | None, dict[str, object]],
) -> dict[str, object] | None:
    """把库里的旧判定回填成**等效** free_payload。

    upsert 会用 :meth:`WindowParser.parse` 重算 free_type / free_end_at——
    本轮没复查的条目若不带旧值，会被算成 UNKNOWN，**把正确的判定抹掉**。
    """
    free_type, free_end_at, payload = state
    checked_at = (
        payload.get(_PROMO_CHECKED_AT_KEY) if isinstance(payload, dict) else None
    )
    if free_type in (FreeType.PERMANENT, FreeType.TIMED):
        result: dict[str, object] = {"free_classes": ["free"]}
        if free_end_at:
            result["remaining_abs"] = free_end_at
        if checked_at:
            result[_PROMO_CHECKED_AT_KEY] = checked_at
        return result
    if checked_at:
        # 已确认非免费：保留标记（否则下一轮又会被当成「新条目」）。
        # ``free_classes=[]`` 让 WindowParser 直接落 UNKNOWN，不走文本兜底。
        result = dict(payload)
        result.setdefault("free_classes", [])
        return result
    return None


def _payload_from_promotion(
    promo: Promotion | None,
    checked_at: str,
) -> dict[str, object]:
    """站点的当前生效优惠 → 等效 free_payload。

    ``None`` = 当前不免费 → 写入「已查非免费」标记；不再每轮重查。
    """
    if promo is None or promo.kind.lower() != "free":
        return {
            "free_classes": [],
            _PROMO_CHECKED_AT_KEY: checked_at,
        }
    payload: dict[str, object] = {
        "free_classes": ["free"],
        _PROMO_CHECKED_AT_KEY: checked_at,
    }
    if promo.end_at:
        payload["remaining_abs"] = str(promo.end_at)
    return payload


class _PromoResolution(NamedTuple):
    """优惠复查的结果。"""

    #: 补上免费判定（``free_payload``）后的条目列表
    items: list[FeedItem]
    #: 熔断原因（非 None = 站点被限流，本轮复查已中止）
    breaker: str | None = None
    #: **站点级**永久失败原因（非 None = 凭据失效等，本轮复查已中止）
    error: str | None = None


async def _resolve_promotions(
    adapter: SiteAdapter, row: Site, feed_items: list[FeedItem]
) -> _PromoResolution:
    """给 feed_items 补上**真实**的免费判定（返回带 free_payload 的新列表）。

    只有支持优惠查询的适配器（``supports_promotion_lookup``）走这条路：
    多数 NexusPHP 站把 ``pro_*`` 优惠标记直接 SSR 在列表 HTML 里，adapter
    解析列表时就拿到了；hdwing 的优惠标记是 AJAX 占位符，只能逐种子查
    服务端渲染的优惠历史表（``promotion.php``）——**那个 AJAX 接口不可用**
    （Cloudflare Turnstile 闸，未过闸时对任意 id 都返回兜底的「免费」）。

    **增量**：只复查「新增条目」「已过期条目」与「距上次非免费判定超过
    TTL 的条目」，其余沿用库里旧判定（回填成等效 free_payload，upsert 后
    值不变）。否则每轮都要对几百个种子逐个请求站点——既慢又会被限流。

    **故障分级**（传输层异常由 adapter 统一翻译成本层异常，service 不碰
    HTTP 实现细节）：

    - :class:`SitePermanentError`（登录页 → cookie 失效）= **站点级**故障
      → 中止本轮复查并上报站点失败。不能当单条失败吞掉：那会把故障静默
      写成「非免费」，且下一轮还会对同一批失效 cookie 重打上百个请求。
    - :class:`SiteFetchError`（网络 / HTTP / 解析）= 单条瞬时故障
      → 该条目本轮不写判定，下轮重试。
    - :class:`BreakerTripped`（403/429）= 熔断整站。

    Returns: :class:`_PromoResolution`——``breaker`` 非 None = 站点被限流，
    ``error`` 非 None = 站点级永久失败；两者都会中止本轮复查。
    """
    if not feed_items:
        return _PromoResolution(items=feed_items)
    if not getattr(adapter, "supports_promotion_lookup", False):
        return _PromoResolution(items=feed_items)

    now = bj_now_dt()
    known = await _known_free_states(row.id)
    payloads: dict[int, dict[str, object]] = {}
    pending: list[tuple[int, FeedItem]] = []
    for idx, fi in enumerate(feed_items):
        item_id = (fi.item_id or "").strip()
        if not item_id:
            continue
        old = known.get(item_id)
        if old is not None and not _needs_recheck(old, now):
            payload = _payload_from_state(old)
            if payload is not None:
                payloads[idx] = payload
            continue
        pending.append((idx, fi))
    if not pending:
        resolved = [
            replace(fi, free_payload=payloads[idx]) if idx in payloads else fi
            for idx, fi in enumerate(feed_items)
        ]
        return _PromoResolution(items=resolved)

    limited = pending[:_PROMO_LOOKUP_MAX]
    logger.info(
        "站点 %s 优惠复查 %d 条（沿用旧判定 %d 条）",
        row.name,
        len(limited),
        len(feed_items) - len(pending),
    )
    if len(pending) > len(limited):
        logger.info(
            "站点 %s 本轮复查优惠 %d/%d 条（超上限，剩余下轮继续）",
            row.name,
            len(limited),
            len(pending),
        )

    semaphore = asyncio.Semaphore(_PROMO_LOOKUP_CONCURRENCY)
    checked_at = format_bj(now)
    failures = 0
    #: 站点级永久失败（凭据失效）——一旦出现即中止本轮复查
    permanent_error: str | None = None

    async def _one(idx: int, fi: FeedItem) -> None:
        nonlocal failures, permanent_error
        async with semaphore:
            if permanent_error is not None:
                # 已确认凭据失效 → 不再对站点发请求（避免对失效 cookie 反复重打）
                return
            try:
                promo = await adapter.get_promotion(fi.item_id or "")
            except BreakerTripped:
                raise
            except SitePermanentError as exc:
                # 凭据失效 = **站点级**故障：中止本轮复查并上报站点失败。
                # 不能当单条失败吞掉——那会把故障静默伪装成「非免费」。
                permanent_error = str(exc)
                logger.error(
                    "站点 %s 优惠判定永久失败（中止本轮复查）：%s",
                    row.name,
                    exc,
                )
                return
            except SiteFetchError as exc:
                # 单条查失败不影响其它条目：该条目本轮不写免费判定
                failures += 1
                logger.warning(
                    "站点 %s 优惠判定失败 item_id=%s：%s",
                    row.name,
                    fi.item_id,
                    exc,
                )
                return
            payloads[idx] = _payload_from_promotion(promo, checked_at)

    breaker: str | None = None
    results = await asyncio.gather(
        *(_one(idx, fi) for idx, fi in limited),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BreakerTripped):
            logger.warning("站点 %s 优惠判定触发熔断：%s", row.name, result)
            breaker = str(result)
            break
        if isinstance(result, Exception):
            failures += 1
            logger.error(
                "站点 %s 优惠判定出现未预期异常：%s",
                row.name,
                result,
                exc_info=result,
            )

    if failures:
        logger.warning(
            "站点 %s 优惠判定失败 %d/%d 条（本轮不写判定，下轮重试）",
            row.name,
            failures,
            len(limited),
        )

    resolved = [
        replace(fi, free_payload=payloads[idx]) if idx in payloads else fi
        for idx, fi in enumerate(feed_items)
    ]
    return _PromoResolution(items=resolved, breaker=breaker, error=permanent_error)


async def _upsert_site_items(
    row: Site, feed_items: list[FeedItem]
) -> tuple[int, int]:
    """批量 upsert 单站条目（每站一个短事务，避免写锁横跨网络 IO）。"""
    if not feed_items:
        return (0, 0)
    ok = 0
    now = bj_now()
    async with session_scope() as session:
        for fi in feed_items:
            item_id = (fi.item_id or "").strip() or None
            window = WindowParser.parse(fi)
            stmt = sqlite_insert(TorrentIndex).values(
                site_id=row.id,
                item_id=item_id,
                # info_hash 不写：list 适配器硬编码 None，会污染 push_free 写的真 hash
                name=fi.title[:1024],
                size=fi.size,
                category=fi.category,
                description=fi.description or None,
                free_type=window.free_type,
                free_end_at=window.free_end_at,
                free_source=window.free_source,
                free_confidence=window.confidence,
                discount_pct=window.discount_pct,
                free_payload=fi.free_payload or {},
                published_at=fi.published_at,
                first_seen_at=now,
                last_seen_at=now,
            ).on_conflict_do_update(
                index_elements=["site_id", "item_id"],
                set_={
                    # info_hash 不覆盖：list 适配器 None 会清空 push_free 写的真 hash
                    "name": fi.title[:1024],
                    "size": fi.size,
                    "category": fi.category,
                    "description": fi.description or None,
                    "free_type": window.free_type,
                    "free_end_at": window.free_end_at,
                    "free_source": window.free_source,
                    "free_confidence": window.confidence,
                    "discount_pct": window.discount_pct,
                    "free_payload": fi.free_payload or {},
                    "published_at": fi.published_at,
                    "last_seen_at": now,
                },
            )
            try:
                # 用 savepoint 隔离单条 upsert 失败——rollback 会回滚
                # 整个未提交事务（包括前序成功的行），导致 ok 计数与实际
                # 入库行数不一致。begin_nested 失败只回滚到 savepoint。
                async with session.begin_nested():
                    await session.execute(stmt)
                    ok += 1
            except Exception as exc:
                logger.debug(
                    "站点 %s item_id=%s upsert 失败：%s", row.name, item_id, exc
                )
    return (ok, 0)


