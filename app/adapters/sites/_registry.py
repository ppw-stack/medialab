"""站点 adapter 注册中心（按字符串名解析类）。

设计：
- 不扫描文件系统（避免恶意文件注入）
- 启动时静态可分析
- 添加新站点 = 1 行字典 + 1 个 .py 文件

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
    "mteam": "app.adapters.sites.mteam.MteamAdapter",
    "hhanclub": "app.adapters.sites.hhanclub.HhanclubAdapter",
    "hdsky": "app.adapters.sites.hdsky.HdskyAdapter",
    "ourbits": "app.adapters.sites.ourbits.OurbitsAdapter",
    "audiences": "app.adapters.sites.audiences.AudiencesAdapter",
    "springsunday": "app.adapters.sites.springsunday.SpringsundayAdapter",
    "hdhome": "app.adapters.sites.hdhome.HdhomeAdapter",
    "hddolby": "app.adapters.sites.hddolby.HddolbyAdapter",
}

#: 展示用元数据（label + 默认域名）。键集合必须与 ``_BUILTIN`` 一致。
#: 任何 _BUILTIN 变更需同步这里；启动期 check_meta_consistency 校验。
_META: dict[str, AdapterMeta] = {
    "mteam":        AdapterMeta("M-team",        "m-team.io"),
    "hhanclub":     AdapterMeta("HHanClub",     "hhanclub.net"),
    "hdsky":        AdapterMeta("HDSky",        "hdsky.me"),
    "ourbits":      AdapterMeta("OurBits",      "ourbits.club"),
    "audiences":    AdapterMeta("Audiences",    "audiences.me"),
    "springsunday": AdapterMeta("Springsunday", "springsunday.net"),
    "hdhome":       AdapterMeta("HDHome",       "hdhome.org"),
    "hddolby":      AdapterMeta("HDDolby",      "hddolby.com"),
}

# 启动时检查两个 dict 同步（开发期防御）
assert set(_BUILTIN) == set(_META), (
    f"adapter 注册表不一致：_BUILTIN={set(_BUILTIN) - set(_META)} "
    f"_META={set(_META) - set(_BUILTIN)}"
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
