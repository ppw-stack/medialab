"""站点用户数据契约（FR-1.6，精简版 + 时魔）。

3 业务字段：

- **上传量** (upload, bytes)
- **下载量** (download, bytes)
- **做种数** (seeding_count, 当前做种)

**时魔**（每小时获得的站点积分）：各站叫法不同（魔力值 / 爆米花 / 茉莉 /
憨豆 / 积分），统一用 :class:`HourlyBonus` 表达，``unit`` 标注站点自称。

时魔取**合计口径**——站点每小时实际能拿到的总额（含官种加成 / 勋章加成 /
后宫加成等各项加成），不是「基础时魔」。各站取法见
:mod:`app.adapters.sites.parsers.nexus_php` 与各站适配器。

其他字段（username/user_level/bonus/H&R/消息/做种积分/**下载中数**）已移除：
适配层 4 方法合并为单方法 :meth:`SiteAdapter.get_user_snapshot`，
service 只 upsert 这几列。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HourlyBonus", "SiteUserSnapshot"]


@dataclass(frozen=True, slots=True)
class HourlyBonus:
    """时魔（每小时获得的站点积分·合计口径）。

    :param amount: 时魔数值（每小时的收益，各站单位不同）
    :param unit: 站点对该积分的称呼（"魔力值" / "爆米花" / "茉莉" / "憨豆" / "积分"）
    :param capped: 是否已达该站的获取上限。
        ``None`` = 该站页面不提供上限信号（无法判断），不是「没封顶」。
    :param progress: 站点进度百分比（>=100 表示封顶）。
        口径由站点自己定（通常相对**基础时魔**算），与 ``amount`` 的
        合计口径不一定同源。
    """

    amount: float
    unit: str | None = None
    capped: bool | None = None
    progress: float | None = None


@dataclass(frozen=True, slots=True)
class SiteUserSnapshot:
    """单次 refresh 拉到的业务字段（FR-1.6 精简版 + 时魔）。"""

    upload: int = 0
    download: int = 0
    seeding_count: int = 0
    #: 时魔（各站单位不同，见 :class:`HourlyBonus`）；该站不支持则 ``None``
    hourly_bonus: HourlyBonus | None = None
