"""时魔解析契约测试（FR-1.6 增补）。

解析规则**必须用真实页面固化**（AGENTS.md）：手写最小 HTML 会漏掉真实
文案与 DOM 坑。各站实测差异（本文件就是这些坑的清单）：

============  ==================  ==================  ==========================
站点          时魔口径            实测值              上限信号
============  ==================  ==================  ==========================
hdsky         headline（无合计表）  89.563              进度条 11.78%
ourbits       headline（无合计表）  90.557              进度条 95.32%
soulvoice     合计表「合计」列     151.444             进度条 94.65%
hdhome        headline + 额外奖励   92.898              进度条 55.45%（共 3 根）
hhanclub      div 网格合计（跨行）  58.711              进度条 37.12%
audiences     侧栏值（已含加成）   154.0               文案「已达上限」+ 110%
springsunday  表「每小时茉莉」列   105.571             无 → 上限未知
============  ==================  ==================  ==========================

m-team 是 API 站，**没有时魔**（``memberCount.bonus`` 是累计余额，不是
每小时收益）。
"""

from __future__ import annotations

import pathlib

import pytest
from app.adapters.sites import audiences as audiences_mod
from app.adapters.sites import hdhome as hdhome_mod
from app.adapters.sites import hhanclub as hhanclub_mod
from app.adapters.sites import springsunday as springsunday_mod
from app.adapters.sites.parsers import nexus_php
from app.adapters.sites.user_data import HourlyBonus, SiteUserSnapshot

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"

#: 站点 → (fixture 文件, 解析入口, 期望时魔, 单位, capped, progress)
#
#: 进度是**站点自己算的**（相对基础时魔），每次刷新都在动——断言用区间，
#: 不写死具体值（写死会因站点重算而漂移）。
_STANDARD = (
    "hdsky",
    "ourbits",
    "soulvoice",
    "hdhome",
)


def _read(name: str) -> str:
    path = FIXTURES / f"{name}_mybonus.html"
    assert path.exists(), f"缺少 fixture：{path}"
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 标准模板


@pytest.mark.parametrize("name", _STANDARD)
def test_standard_site_parses_hourly_bonus(name: str) -> None:
    """标准 NexusPHP 站：时魔 + 单位 + 进度。"""
    html = _read(name)
    bonus = nexus_php.parse_hourly_bonus(html, unit="魔力值")
    assert bonus is not None
    assert bonus.amount > 0
    assert bonus.unit == "魔力值"
    assert bonus.progress is not None
    assert 0 < bonus.progress < 100
    assert bonus.capped is False


def test_soulvoice_uses_total_column_not_headline() -> None:
    """soulvoice 有「每小时获得的合计」表——时魔取**合计列**。

    合计列与 headline 数值相同（官种加成为 0），但取法不同：合计列才是
    「含各项加成」的口径（hhanclub 上两者相差 3 倍，见下）。
    """
    html = _read("soulvoice")
    total = nexus_php.parse_bonus_total_column(html)
    assert total is not None
    bonus = nexus_php.parse_hourly_bonus(html, unit="魔力值")
    assert bonus is not None
    assert bonus.amount == pytest.approx(total)


def test_headline_fallback_when_no_total_table() -> None:
    """hdsky / ourbits 没有合计表 → 时魔 = headline。"""
    for name in ("hdsky", "ourbits"):
        html = _read(name)
        assert nexus_php.parse_bonus_total_column(html) is None
        headline = nexus_php.parse_bonus_headline(html)
        assert headline is not None
        bonus = nexus_php.parse_hourly_bonus(html, unit="魔力值")
        assert bonus is not None
        assert bonus.amount == pytest.approx(headline[0])


def test_progress_read_from_alt_attribute_not_style() -> None:
    """进度条百分比取 ``alt="NN%"``——``style`` 的单位各站不同（px / %）。"""
    html = _read("hhanclub")
    progress = nexus_php.parse_progress_percent(html)
    assert progress is not None
    assert 0 < progress < 100


# ------------------------------------------------------------------ hhanclub


def test_hhanclub_total_from_row_span_cell() -> None:
    """hhanclub 的合计值在 div 网格的**跨行单元格**里（不是 table）。

    headline（「你当前每小时能获取X个积分」）只是**基础**值，不含官种 /
    勋章加成——用合计才是「每小时实际能拿到的总数」。
    """
    html = _read("hhanclub")
    headline = nexus_php.parse_bonus_headline(html)
    assert headline is not None
    bonus = hhanclub_mod._parse_hhanclub_hourly_bonus(html)
    assert bonus is not None
    assert bonus.unit == "憨豆"
    # 合计 > 基础（官种加成 + 勋章加成）
    assert bonus.amount > headline[0]
    assert bonus.progress is not None
    assert bonus.capped is False


# -------------------------------------------------------------------- hdhome


def test_hdhome_total_adds_extra_rewards() -> None:
    """hdhome 的 headline 是**基础**时魔，合计要加各项额外奖励。

    页面把额外奖励单列（「黄星时魔奖励（0）」/「…额外奖励（4.181）」），
    headline 自己标了「【基础时魔/做种积分】」。
    """
    html = _read("hdhome")
    headline = nexus_php.parse_bonus_headline(html)
    assert headline is not None
    bonus = hdhome_mod._parse_hdhome_hourly_bonus(html)
    assert bonus is not None
    assert bonus.amount > headline[0]
    assert bonus.unit == "魔力值"
    # 3 根进度条，第 1 根是总进度
    assert bonus.progress is not None
    assert 0 < bonus.progress < 100


# ----------------------------------------------------------------- audiences


def test_audiences_side_panel_value_and_capped() -> None:
    """audiences 是 SPA：时魔在侧栏，进度与「已达上限」文案都在侧栏。"""
    html = _read("audiences")
    bonus = audiences_mod._parse_audiences_hourly_bonus(html)
    assert bonus is not None
    assert bonus.amount > 0
    assert bonus.unit == "爆米花"
    assert bonus.progress is not None
    assert bonus.progress >= 100
    assert bonus.capped is True


# -------------------------------------------------------------- springsunday


def test_springsunday_hourly_bomoli_column() -> None:
    """springsunday 的时魔在「当前每小时能获得的积分/茉莉」表的
    「每小时茉莉」列（**我的数据**行，含加成系数）。"""
    html = _read("springsunday")
    bonus = springsunday_mod._parse_springsunday_hourly_bonus(html)
    assert bonus is not None
    assert bonus.unit == "茉莉"
    assert bonus.amount > 0
    # 没有进度指示 → 上限无法判断
    assert bonus.capped is None
    assert bonus.progress is None


# ---------------------------------------------------------- 4 业务字段（navbar）


@pytest.mark.parametrize("name", _STANDARD)
def test_standard_site_parses_business_fields(name: str) -> None:
    """mybonus.php 的 navbar 与 userdetails.php 同一套——4 业务字段照常可解析。"""
    html = _read(name)
    snap = nexus_php.parse_user_snapshot(html)
    assert snap is not None
    assert snap.upload > 0
    assert snap.download > 0
    assert snap.seeding_count > 0


def test_hhanclub_parses_business_fields() -> None:
    """hhanclub 用自己的 navbar 解析（``<img alt="上传">`` 模式）。"""
    html = _read("hhanclub")
    snap = hhanclub_mod._parse_hhanclub_snapshot(html)
    assert snap is not None
    assert snap.upload > 0
    assert snap.download > 0
    assert snap.seeding_count > 0


# ------------------------------------------------------------------ 兜底行为


def test_parse_hourly_bonus_returns_none_on_garbage() -> None:
    """页面里没有时魔 → 返回 ``None``（不是 0）。"""
    assert nexus_php.parse_hourly_bonus("<html><body>空页面</body></html>") is None
    assert nexus_php.parse_bonus_headline("<html></html>") is None
    assert nexus_php.parse_progress_percent("<html></html>") is None
    assert nexus_php.parse_bonus_total_column("<html></html>") is None


def test_hourly_bonus_dataclass_defaults() -> None:
    """契约：``capped=None`` 表示「无法判断」，不是「没封顶」。"""
    bonus = HourlyBonus(amount=1.5)
    assert bonus.unit is None
    assert bonus.capped is None
    assert bonus.progress is None
    assert SiteUserSnapshot().hourly_bonus is None
