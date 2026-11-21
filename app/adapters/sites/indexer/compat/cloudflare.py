"""Cloudflare / DDoS-GUARD 挑战页识别（移植自 MoviePilot）。"""

from __future__ import annotations

from parsel import Selector

CHALLENGE_TITLES = (
    "Just a moment...",
    "请稍候…",
    "DDOS-GUARD",
)

CHALLENGE_SELECTORS = (
    "#cf-challenge-running",
    ".ray_id",
    ".attack-box",
    "#cf-please-wait",
    "#challenge-spinner",
    "#trk_jschal_js",
    "td.info #js_info",
    "div.vc div.text-box h2",
)

#: 标题的小写集合——模块级建一次，不要每次调用都重建集合。
_CHALLENGE_TITLES_LOWER = frozenset(title.lower() for title in CHALLENGE_TITLES)


def under_challenge(html_text: str) -> bool:
    """页面标题或选择器命中已知挑战页时返回 True。"""
    if not html_text:
        return False
    doc = Selector(text=html_text, type="html")
    title = (doc.css("title::text").get() or "").strip()
    if title.lower() in _CHALLENGE_TITLES_LOWER:
        return True
    return any(doc.css(sel) for sel in CHALLENGE_SELECTORS)
