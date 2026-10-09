"""告警中心（FR-4.3）。

四条策略缺一不可，任意一条失效都会让告警变成噪音：

1. **去重与聚合**：同一 hash + 同一告警类型在 ``mute_window``（默认 6h）
   内不重复推送。
2. **分级静默**：🟡 每日汇总一次；🔴 立即推送。
3. **风暴保护**：单日告警超过阈值（默认 50）自动降级为汇总推送。
4. **静默时段**：免打扰时段内 🟡 延迟，🔴 **不受限制**。

第 4 条的例外是刻意的：夜间正是端口被关、限速被改的时段，🔴 若被静默
就会错过 H&R 风险。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.notify import AlertPayload, NotifyChannelAdapter, PanelNotifier, SendResult
from app.core.logging import get_logger
from app.core.metrics import alerts_sent
from app.core.timeutils import utcnow
from app.db.models import Alert, AlertLevel, NotifyChannel

logger = get_logger(__name__)

#: 渠道连续失败多少次后熔断
CHANNEL_BREAKER_THRESHOLD = 5
#: 渠道熔断时长
CHANNEL_BREAKER_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class AlertPolicyConfig:
    """告警策略（FR-4.3）。"""

    mute_window_hours: int = 6
    warning_daily_digest: bool = True
    critical_immediate: bool = True
    storm_threshold: int = 50
    quiet_hours: str | None = None

    @property
    def mute_window(self) -> timedelta:
        return timedelta(hours=max(self.mute_window_hours, 0))


@dataclass
class DispatchPlan:
    """一次告警的投递决策。"""

    alerts: list[AlertPayload] = field(default_factory=list)
    #: 被去重压制的数量
    suppressed: int = 0
    #: 是否触发风暴保护（全部降级为汇总）
    storm: bool = False
    #: 是否处于静默时段（🟡 被延迟）
    quiet: bool = False

    @property
    def should_send(self) -> bool:
        return bool(self.alerts)


def parse_quiet_hours(spec: str | None) -> tuple[time, time] | None:
    """解析 ``HH:MM-HH:MM``。

    支持跨零点（``23:00-07:00`` 表示夜段）。
    """
    if not spec:
        return None
    parts = spec.split("-")
    if len(parts) != 2:
        return None
    try:
        start = time.fromisoformat(parts[0].strip())
        end = time.fromisoformat(parts[1].strip())
    except ValueError:
        return None
    return start, end


def in_quiet_hours(spec: str | None, at: datetime) -> bool:
    """当前是否处于静默时段。"""
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    now = at.time()
    if start <= end:
        return start <= now < end
    # 跨零点
    return now >= start or now < end


def dedupe_key(category: str, info_hash: str | None) -> str:
    """去重键：``category:info_hash``（FR-4.3）。"""
    return f"{category}:{info_hash or '-'}"


class Alerter:
    """告警中心。"""

    def __init__(
        self,
        session: AsyncSession,
        policy: AlertPolicyConfig | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or AlertPolicyConfig()
        self._channels_cache: list[NotifyChannel] | None = None

    @property
    def policy(self) -> AlertPolicyConfig:
        return self._policy

    # ------------------------------------------------------------ 去重
    async def _is_muted(self, key: str, now: datetime) -> bool:
        """该 key 是否仍在静默窗口内。

        仅以 ``muted_until`` 为权威（FR-4.3 落库时设置）。该字段为 NULL
        即视为**非静默**——避免``ts`` 与 ``muted_until`` 漂移造成的
        "永远沉默"或"窗口永不过期"两类缺陷。
        """
        row = (
            await self._session.execute(
                select(Alert.muted_until)
                .where(Alert.dedupe_key == key)
                .order_by(Alert.ts.desc())
                .limit(1)
            )
        ).first()
        if row is None or row[0] is None:
            return False
        return row[0] > now

    async def today_count(self, now: datetime | None = None) -> int:
        """今日告警数（风暴保护判定，FR-4.3）。"""
        ref = now or utcnow()
        start = datetime.combine(ref.date(), time.min)
        return (
            await self._session.execute(
                select(func.count(Alert.id)).where(Alert.ts >= start)
            )
        ).scalar_one()

    # ------------------------------------------------------------ 决策
    async def plan(
        self,
        candidates: list[tuple[str, str, str | None]],
        *,
        now: datetime | None = None,
    ) -> DispatchPlan:
        """对待发告警列表做完整决策。

        ``candidates`` 为 ``(level, category, info_hash)`` 三元组列表。
        """
        ref = now or utcnow()
        plan = DispatchPlan()

        total = await self.today_count(ref)
        plan.storm = total >= self._policy.storm_threshold

        quiet = in_quiet_hours(self._policy.quiet_hours, ref)
        plan.quiet = quiet

        if plan.storm:
            # 风暴保护：全部降级为一条汇总（FR-4.3）
            plan.alerts.append(
                AlertPayload(
                    level="warning",
                    category="storm",
                    title="告警风暴",
                    message=f"今日已产生 {total} 条告警，超过阈值 "
                    f"{self._policy.storm_threshold}，已降级为汇总推送。",
                )
            )
            return plan

        by_key: dict[str, int] = defaultdict(int)
        # 批量预取所有候选 key 的最新 muted_until（避免 N+1 查询）
        keys = [dedupe_key(category, info_hash) for _, category, info_hash in candidates]
        muted_map: dict[str, datetime | None] = {}
        if keys:
            rows = (
                await self._session.execute(
                    select(Alert.dedupe_key, Alert.muted_until)
                    .where(Alert.dedupe_key.in_(keys))
                    .order_by(Alert.dedupe_key, Alert.ts.desc())
                )
            ).all()
            for k, mu in rows:
                if k not in muted_map:  # 取最新（ts desc）的一条
                    muted_map[k] = mu

        for level, category, info_hash in candidates:
            key = dedupe_key(category, info_hash)
            mu = muted_map.get(key)
            if mu is not None and mu > ref:
                plan.suppressed += 1
                continue

            # 🔴 绕过静默时段与每日汇总（FR-4.3）
            if level == AlertLevel.CRITICAL and self._policy.critical_immediate:
                by_key[key] += 1
                continue

            if level == AlertLevel.WARNING and self._policy.warning_daily_digest:
                by_key[key] += 1
                continue

            if quiet and level != AlertLevel.CRITICAL:
                plan.suppressed += 1
                continue

            by_key[key] += 1

        for (category, info_hash), count in self._expand(by_key).items():
            plan.alerts.append(
                AlertPayload(
                    level=AlertLevel.WARNING,
                    category=category,
                    info_hash=info_hash,
                    title=category,
                    message=f"{count} 个对象触发同一告警",
                )
            )
        return plan

    @staticmethod
    def _expand(by_key: dict[str, int]) -> dict[tuple[str, str | None], int]:
        out: dict[tuple[str, str | None], int] = {}
        for key, count in by_key.items():
            category, _, info_hash = key.partition(":")
            pair = (category, None if info_hash == "-" else info_hash)
            out[pair] = out.get(pair, 0) + count
        return out

    # ------------------------------------------------------------ 落库
    async def record(
        self,
        level: str,
        category: str,
        message: str,
        *,
        info_hash: str | None = None,
        torrent_id: int | None = None,
        now: datetime | None = None,
    ) -> Alert:
        """告警落库并设置静默窗口。

        **无论是否推送都要落库**：面板需要完整历史，而静默窗口的计时
        依赖落库记录。只在"真的发出去了"才记会让 mute 完全失效。
        """
        ref = now or utcnow()
        key = dedupe_key(category, info_hash)
        alert = Alert(
            ts=ref,
            level=level,
            category=category,
            info_hash=info_hash,
            torrent_id=torrent_id,
            message=message[:4000],
            dedupe_key=key,
            muted_until=ref + self._policy.mute_window,
        )
        self._session.add(alert)
        await self._session.flush()
        return alert

    # ------------------------------------------------------------ 投递
    async def dispatch(self, payload: AlertPayload) -> dict[str, bool]:
        """多渠道并行投递（FR-4.2）。

        单个渠道失败只记失败计数与熔断，**不得**影响其他渠道或巡检主流程。
        """
        results: dict[str, bool] = {}
        now = utcnow()
        if self._channels_cache is None:
            self._channels_cache = (
                (
                    await self._session.execute(
                        select(NotifyChannel).where(NotifyChannel.enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
        channels = self._channels_cache

        for ch in channels:
            if ch.breaker_until and ch.breaker_until > now:
                results[ch.name] = False
                continue
            if AlertLevel.rank(payload.level) < AlertLevel.rank(ch.min_level):
                results[ch.name] = False
                continue

            ok = await self._deliver(ch, payload)
            results[ch.name] = ok
            if ok:
                alerts_sent.labels(level=payload.level, channel=ch.type).inc()
        return results

    async def _deliver(self, ch: NotifyChannel, payload: AlertPayload) -> bool:
        """单渠道投递 + 失败计数 / 熔断。"""
        from app.adapters.notify import build_adapter
        from app.services.factory import decrypt_notify_secret

        secret: str | None = None
        if ch.secret_enc:
            secret = decrypt_notify_secret(ch, ch.secret_enc)
        adapter = build_adapter(ch.type, dict(ch.config or {}), secret)

        try:
            result = await adapter.send(payload)
        except Exception as exc:
            logger.warning("渠道 %s 投递异常：%s", ch.name, exc)
            result = SendResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if result.ok:
            ch.fail_count = 0
            ch.last_ok_at = utcnow()
            ch.last_error = None
        else:
            ch.fail_count += 1
            ch.last_error = result.error[:500]
            if ch.fail_count >= CHANNEL_BREAKER_THRESHOLD:
                ch.breaker_until = utcnow() + timedelta(seconds=CHANNEL_BREAKER_SECONDS)
                logger.warning(
                    "渠道 %s 连续失败 %d 次，熔断 %ds",
                    ch.name,
                    ch.fail_count,
                    CHANNEL_BREAKER_SECONDS,
                )
        await self._session.flush()
        return result.ok

    # ------------------------------------------------------------ 查询
    async def today_stats(self, now: datetime | None = None) -> dict[str, int]:
        """今日各级别计数（面板仪表盘）。"""
        ref = now or utcnow()
        start = datetime.combine(ref.date(), time.min)
        rows = (
            (
                await self._session.execute(
                    select(Alert.level, func.count(Alert.id))
                    .where(Alert.ts >= start)
                    .group_by(Alert.level)
                )
            )
            .all()
        )
        return dict(rows)


__all__ = [
    "AlertPolicyConfig",
    "Alerter",
    "DispatchPlan",
    "NotifyChannelAdapter",
    "PanelNotifier",
    "dedupe_key",
    "in_quiet_hours",
    "parse_quiet_hours",
]
