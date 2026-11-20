"""MoviePilot 移植代码的日志适配。

被移植的 parser/spider 使用 ``logger.info/warn/debug``，这里提供一个
标准库 Logger 别名，接入本项目统一的日志配置。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("medialab.site_indexer")
