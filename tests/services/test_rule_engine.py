"""规则引擎测试（FR-5.1）。"""

from __future__ import annotations

from datetime import timedelta

from app.core.timeutils import utcnow
from app.db.models import CleanupRule, Source, TorrentStatus
from app.services.cleanup import RuleEngine


def _t(**kw):
    """构造一个测试用的 Torrent-like 命名空间。"""
    defaults = {
        "info_hash": "a" * 40,
        "source": Source.AUTO_SEED,
        "status": TorrentStatus.ACTIVE,
        "ratio": 1.0,
        "size": 1024**3,
        "added_at": utcnow() - timedelta(days=10),
        "category": "movie",
        "tags": "",
        "site_id": 1,
    }
    defaults.update(kw)
    ns = type("T", (), defaults)()
    return ns


def _rule(when: dict) -> CleanupRule:
    return CleanupRule(id=1, name="r", when=when, priority=100, enabled=True)


# ------------------------------------------------------------ source 维度
def test_source_in_csv() -> None:
    rule = _rule({"source_in": "auto_seed,free_seed"})
    assert RuleEngine.matches(rule, _t(source=Source.AUTO_SEED))
    assert RuleEngine.matches(rule, _t(source=Source.FREE_SEED))
    assert not RuleEngine.matches(rule, _t(source=Source.MANUAL))


def test_source_in_list() -> None:
    rule = _rule({"source_in": ["auto_seed"]})
    assert RuleEngine.matches(rule, _t(source=Source.AUTO_SEED))
    assert not RuleEngine.matches(rule, _t(source=Source.MANUAL))


# ------------------------------------------------------------ ratio
def test_ratio_gte() -> None:
    rule = _rule({"ratio_gte": 2.0})
    assert RuleEngine.matches(rule, _t(ratio=2.0))
    assert RuleEngine.matches(rule, _t(ratio=5.0))
    assert not RuleEngine.matches(rule, _t(ratio=1.5))


def test_ratio_lte() -> None:
    rule = _rule({"ratio_lte": 1.0})
    assert RuleEngine.matches(rule, _t(ratio=0.5))
    assert not RuleEngine.matches(rule, _t(ratio=2.0))


# ------------------------------------------------------------ 时间维度
def test_seeding_days_gte() -> None:
    rule = _rule({"seeding_days_gte": 7})
    t = _t(added_at=utcnow() - timedelta(days=10))
    assert RuleEngine.matches(rule, t)
    t2 = _t(added_at=utcnow() - timedelta(days=3))
    assert not RuleEngine.matches(rule, t2)


def test_seeding_days_with_null_added_at() -> None:
    """未填 added_at：按 ref 计算 → 0 天，应小于阈值不命中。"""
    rule = _rule({"seeding_days_gte": 1})
    assert not RuleEngine.matches(rule, _t(added_at=None))


# ------------------------------------------------------------ 大小
def test_size_gte() -> None:
    rule = _rule({"size_gte": 5 * 1024**3})  # 5 GB
    assert RuleEngine.matches(rule, _t(size=10 * 1024**3))
    assert not RuleEngine.matches(rule, _t(size=1 * 1024**3))


# ------------------------------------------------------------ 分类/标签
def test_category_eq() -> None:
    rule = _rule({"category_eq": "movie"})
    assert RuleEngine.matches(rule, _t(category="movie"))
    assert not RuleEngine.matches(rule, _t(category="tv"))


def test_category_in_csv() -> None:
    rule = _rule({"category_in": "movie,tv"})
    assert RuleEngine.matches(rule, _t(category="movie"))
    assert not RuleEngine.matches(rule, _t(category="doc"))


def test_tag_in() -> None:
    rule = _rule({"tag_in": "free-seed,site-a"})
    assert RuleEngine.matches(rule, _t(tags="free-seed,auto"))
    assert not RuleEngine.matches(rule, _t(tags="other"))


# ------------------------------------------------------------ 站点
def test_site_id() -> None:
    rule = _rule({"site_id": 3})
    assert RuleEngine.matches(rule, _t(site_id=3))
    assert not RuleEngine.matches(rule, _t(site_id=5))


# ------------------------------------------------------------ 状态
def test_status_not() -> None:
    rule = _rule({"status_not": TorrentStatus.DELETED})
    assert RuleEngine.matches(rule, _t(status=TorrentStatus.ACTIVE))
    assert not RuleEngine.matches(rule, _t(status=TorrentStatus.DELETED))


def test_status_in_csv() -> None:
    rule = _rule({"status": "active,paused"})
    assert RuleEngine.matches(rule, _t(status=TorrentStatus.ACTIVE))
    assert RuleEngine.matches(rule, _t(status=TorrentStatus.PAUSED))
    assert not RuleEngine.matches(rule, _t(status=TorrentStatus.DELETED))


# ------------------------------------------------------------ 组合
def test_all_must_match() -> None:
    """DSL 是 AND 关系：全部命中才匹配。"""
    rule = _rule({"ratio_gte": 3.0, "size_gte": 1024**3})
    assert RuleEngine.matches(rule, _t(ratio=5.0, size=5 * 1024**3))
    assert not RuleEngine.matches(rule, _t(ratio=5.0, size=1024**2))
    assert not RuleEngine.matches(rule, _t(ratio=1.0, size=5 * 1024**3))


def test_unknown_field_safe_negative() -> None:
    """未知字段→False 而不是抛错（向后兼容 DSL 升级）。"""
    rule = _rule({"unknown_field": 42})
    assert not RuleEngine.matches(rule, _t())


def test_empty_when_matches_all() -> None:
    """空规则匹配所有（无门槛 = 全清——危险默认 → 由 UI 强制要求 at least 1 项）。"""
    assert RuleEngine.matches(_rule({}), _t())


# ------------------------------------------------------------ 真表构造
# real-DB test covered by end-to-end integration in test_cleaner.py
