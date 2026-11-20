"""站点 adapter 注册中心（按字符串名解析类）。

设计：
- 不扫描文件系统（避免恶意文件注入）
- 启动时静态可分析
- 站点差异由规则包（indexer/rules/indexers.json）表达，注册表只保留 generic

调用方：
- :class:`app.services.factory.AdapterFactory.site` 缓存站点 adapter 实例
- :class:`app.api.routes.sites` 校验 SiteCreate.adapter 字段
"""

from __future__ import annotations

import importlib
from typing import NamedTuple

from app.adapters.sites.base import SiteAdapter


class AdapterMeta(NamedTuple):
    """前端展示用的 adapter 元数据。"""

    label: str
    domain: str


#: 已注册站点 adapter 白名单。
#: 键 = Site.adapter 字段值（小写字母数字，下划线），值 = "module.path.ClassName"。
_BUILTIN: dict[str, str] = {
    # 唯一的站点实现：规则包驱动的通用适配器。
    # 站点差异全部由 indexer/rules/indexers.json 里的选择器配置表达。
    "generic": "app.adapters.sites.generic.GenericAdapter",
}

#: 展示用元数据（label + 默认域名）。键集合必须与 ``_BUILTIN`` 一致。
#: 任何 _BUILTIN 变更需同步这里；启动期 check_meta_consistency 校验。
_META: dict[str, AdapterMeta] = {
    "generic": AdapterMeta("通用(规则包)", ""),
}

# 启动时检查两个 dict 同步（开发期防御）
# 用 raise 而非 assert——assert 在 `python -O` 下被剥离，启动期一致性
# 校验失效会导致两份手维护 dict 不同步的 bug 静默漏过。
if set(_BUILTIN) != set(_META):
    raise RuntimeError(
        f"adapter 注册表不一致：_BUILTIN-only={set(_BUILTIN) - set(_META)} "
        f"_META-only={set(_META) - set(_BUILTIN)}"
    )


def resolve(name: str) -> type[SiteAdapter]:
    """按字符串名查找 adapter 类，未注册则抛 ValueError。"""
    if name not in _BUILTIN:
        available = list_available()
        raise ValueError(
            f"未注册的站点 adapter：{name!r}，可选：{available}"
        )
    mod_path, cls_name = _BUILTIN[name].rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    if not isinstance(cls, type) or not issubclass(cls, SiteAdapter):
        raise TypeError(f"{name} 对应的类不是 SiteAdapter 子类：{cls!r}")
    return cls


def list_available() -> list[str]:
    """返回所有已注册 adapter 名称（按插入顺序）。"""
    return list(_BUILTIN.keys())


def list_meta() -> list[tuple[str, AdapterMeta]]:
    """返回所有 adapter 的展示元数据 ``[(name, AdapterMeta), ...]``。"""
    return [(name, _META[name]) for name in _BUILTIN]


__all__ = ["AdapterMeta", "list_available", "list_meta", "resolve"]
