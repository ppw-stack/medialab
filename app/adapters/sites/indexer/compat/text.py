"""文本工具（MoviePilot ``app.foundation.text`` 的精简移植）。

去掉对 ``moviepilot_rust`` 的分词/简繁转换依赖，只保留解析器使用的数值
与文案处理入口。
"""

from __future__ import annotations

import re
from typing import Union


def contains_chinese(value: Union[str, list[str]]) -> bool:
    """判断文本或文本列表里是否含中文字符。"""
    if not value:
        return False
    if isinstance(value, list):
        value = " ".join(value)
    return re.search(r"[\u4e00-\u9fff]", value) is not None


def parse_int(value: str) -> int:
    """解析可能带千位分隔符的整数，失败返回 0。"""
    if value:
        value = value.strip()
    if not value:
        return 0
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return 0


def parse_float(value: str) -> float:
    """解析可能带千位分隔符的浮点数，失败返回 0.0。"""
    if value:
        value = value.strip()
    if not value:
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def parse_bool(value: object, default: bool = False) -> bool:
    """解析常见布尔文本。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if not value:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on", "是"}:
        return True
    if text in {"false", "0", "no", "n", "off", "否"}:
        return False
    return default


def remove_punctuation(
    value: Union[list[str], str],
    replacement: str = "",
    allow_space: bool = False,
) -> Union[list[str], str]:
    """移除历史匹配规则使用的标点与零宽字符。"""
    punctuation = r"[、.。,，·:：;；!！?？'’\"“”()（）\[\]【】「」\-—―\+\|\\_/&#～~]"
    if not value:
        return value
    if isinstance(value, list):
        return [remove_punctuation(item) for item in value]
    normalized = re.sub(
        r"[\u200B-\u200D\uFEFF]",
        "",
        re.sub(punctuation, replacement, value, flags=re.IGNORECASE),
        flags=re.IGNORECASE,
    )
    if not allow_space:
        return re.sub(r"\s+", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()
