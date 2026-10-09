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

from app.adapters.sites.base import SiteAdapter

#: 已注册站点 adapter 白名单。
#: 键 = Site.adapter 字段值（小写字母数字，下划线），值 = "module.path.ClassName"。
_BUILTIN: dict[str, str] = {
    "mteam": "app.adapters.sites.mteam.MteamAdapter",
}


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


__all__ = ["resolve", "list_available"]
