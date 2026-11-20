"""MoviePilot 运行时配置的极简映射。

被移植的解析代码会读取 ``USER_AGENT`` / ``PROXY`` / ``SITE_MESSAGE`` 等
运行时设置。本项目把这些配置收敛到 :mod:`app.core.config`，此处只做映射，
不再引入 MoviePilot 的 settings 体系。
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings

#: 配置为空时的兜底浏览器 UA（`.env` 里 DEFAULT_USER_AGENT 允许为空，
#: 但 m-team 等站的 WAF 会拒绝无 UA 请求并把 JSON 接口返回 HTML）
_FALLBACK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: 页面编码探测的性能档位（移植代码只作透传，本项目解码逻辑自行处理）
_ENCODING_PERFORMANCE_MODE = "balanced"
#: 页面编码探测的最低置信度
_ENCODING_MIN_CONFIDENCE = 0.6


def get_runtime_setting(name: str, default: Any = None) -> Any:
    """读取一个运行时设置；未知键返回 ``default``。"""
    settings = get_settings()
    if name == "USER_AGENT":
        return (settings.default_user_agent or "").strip() or _FALLBACK_UA
    if name == "PROXY":
        proxy = (settings.proxy_url or "").strip()
        return {"http": proxy, "https": proxy} if proxy else None
    if name == "PROXY_SERVER":
        return (settings.proxy_url or "").strip() or None
    if name == "SITE_MESSAGE":
        return True
    if name == "ENCODING_DETECTION_PERFORMANCE_MODE":
        return _ENCODING_PERFORMANCE_MODE
    if name == "ENCODING_DETECTION_MIN_CONFIDENCE":
        return _ENCODING_MIN_CONFIDENCE
    return default
