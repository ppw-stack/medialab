"""站点索引包的加载与查询。

规则数据来自 MoviePilot-Resources 的 ``user.sites.v3.bin``（Fernet 加密的
pickle）。本项目自用，直接把解密后的 JSON 随包分发（``rules/indexers.json``），
避免运行时依赖 MoviePilot 的 Cython 扩展。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_RULES_PATH = Path(__file__).with_name("rules") / "indexers.json"


class SiteRules:
    """已加载的站点规则集。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.version: str | None = payload.get("version")
        self.indexers: dict[str, dict[str, Any]] = payload.get("indexers") or {}

    def get(self, key: str) -> dict[str, Any] | None:
        """按站点 ID 精确查找规则。"""
        return self.indexers.get(key)

    def find_by_domain(self, url: str) -> dict[str, Any] | None:
        """按域名（含 ``ext_domains``）匹配规则。"""
        candidate = url if "//" in url else f"https://{url}"
        host = (urlsplit(candidate).hostname or "").lower()
        if not host:
            return None
        for rule in self.indexers.values():
            domains = [str(rule.get("domain") or "")]
            ext = rule.get("ext_domains")
            if isinstance(ext, list):
                domains.extend(str(d) for d in ext)
            for item in domains:
                try:
                    candidate = item if "//" in item else f"https://{item}"
                    item_host = (urlsplit(candidate).hostname or "").lower()
                except ValueError:
                    # urlsplit 对非法 IPv6 之类会抛 ValueError，脏规则只跳过该条。
                    continue
                if not item_host:
                    continue
                # 精确匹配优先；再允许「子域 ↔ 主域」匹配（m-team.io ↔ xp.m-team.io）
                if (
                    host == item_host
                    or item_host.endswith(f".{host}")
                    or host.endswith(f".{item_host}")
                ):
                    return rule
        return None

    def find(self, value: str) -> dict[str, Any] | None:
        """先按 ID，再按域名匹配。"""
        return self.get(value) or self.find_by_domain(value)


@lru_cache(maxsize=1)
def load_site_rules() -> SiteRules:
    """加载规则包（进程内缓存）。

    :return: 已解析的站点规则集
    :raises RuntimeError: 规则包文件缺失、不可读或 JSON 内容损坏
    """
    try:
        raw = _RULES_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"读取站点规则包失败：{_RULES_PATH}：{exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"站点规则包 JSON 解析失败：{_RULES_PATH}：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"站点规则包结构无效：{_RULES_PATH}：顶层不是对象")
    return SiteRules(payload)


def list_indexers() -> list[dict[str, Any]]:
    """返回全部站点规则。"""
    return list(load_site_rules().indexers.values())
