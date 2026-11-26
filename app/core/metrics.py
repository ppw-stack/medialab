"""Prometheus 指标（FR-8.3）。

指标命名与 §10 验收口径对齐，供面板与 /metrics 共用。
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS

REGISTRY = CollectorRegistry(auto_describe=True)

#: 拉取次数（按站点/结果）
feed_pulls = Counter(
    "seedhelper_feed_pulls_total",
    "站点拉取次数（按站点/结果）",
    labelnames=("site", "result"),
    registry=REGISTRY,
)

#: 辅种添加数
cross_seed_added = Counter(
    "seedhelper_cross_seed_added_total",
    "辅种任务添加数",
    labelnames=("site", "downloader"),
    registry=REGISTRY,
)

#: 免费种成功/中止数（按四分类与中止原因）
free_seed_outcomes = Counter(
    "seedhelper_free_seed_outcomes_total",
    "免费种结果数",
    labelnames=("outcome", "reason"),
    registry=REGISTRY,
)

#: 清理任务数
cleanup_actions = Counter(
    "seedhelper_cleanup_actions_total",
    "清理动作数",
    labelnames=("action", "trigger"),
    registry=REGISTRY,
)

#: 各阶段耗时
stage_duration = Histogram(
    "seedhelper_stage_duration_seconds",
    "各阶段耗时",
    labelnames=("stage",),
    buckets=(0.05, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120),
    registry=REGISTRY,
)

#: 站点熔断状态（按站点名：1=熔断中，0=正常）
site_breaker_open = Gauge(
    "seedhelper_site_breaker_open",
    "站点是否处于熔断期（1=熔断中，0=正常）",
    labelnames=("site",),
    registry=REGISTRY,
)

#: 实时状态 Gauge
state = {
    "downloaders_up": Gauge(
        "seedhelper_downloaders_up", "下载器在线数", registry=REGISTRY
    ),
    "free_seed_active": Gauge(
        "seedhelper_free_seed_active", "在途免费种任务数", registry=REGISTRY
    ),
    "free_seed_max_active": Gauge(
        "seedhelper_free_seed_max_active",
        "免费种在途下载水位上限（0=不限）",
        registry=REGISTRY,
    ),
    "sites_breaker_open": Gauge(
        "seedhelper_sites_breaker_open",
        "当前处于熔断期的启用站点数量",
        registry=REGISTRY,
    ),
    "free_window_parse_success": Gauge(
        "seedhelper_free_window_parse_success",
        "窗口解析成功率",
        registry=REGISTRY,
    ),
    "db_healthy": Gauge(
        "seedhelper_db_healthy", "数据库健康（1/0）", registry=REGISTRY
    ),
}


def render_metrics() -> tuple[bytes, str]:
    """渲染 Prometheus 文本格式。"""
    return generate_latest(REGISTRY), OPENMETRICS