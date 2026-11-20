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


def under_challenge(html_text: str) -> bool:
    """页面标题或选择器命中已知挑战页时返回 True。"""
    if not html_text:
        return False
    doc = Selector(text=html_text)
    title = (doc.css("title::text").get() or "").strip()
    if title.lower() in {t.lower() for t in CHALLENGE_TITLES}:
        return True
    return any(doc.css(sel) for sel in CHALLENGE_SELECTORS)
