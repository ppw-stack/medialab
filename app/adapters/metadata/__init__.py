"""元数据适配层（TMDB 客户端 / NFO 生成 / 命名规则）。

这一层只做**与外部数据源 / 外部格式打交道**的事：

- :mod:`app.adapters.metadata.tmdb` —— TMDB 官方 API
- :mod:`app.adapters.metadata.nfo_builder` —— Kodi 风格 NFO 文档
- :mod:`app.adapters.metadata.naming` —— 媒体库目录约定（文件名 / 落盘位置）

**不依赖 db / services**（``tests/unit/test_layering.py`` 固化），可独立单测。
"""

from __future__ import annotations

__all__: list[str] = []
