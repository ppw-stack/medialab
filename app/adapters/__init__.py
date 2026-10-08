"""适配层（§7.1）：下载器适配（FR-1）与站点适配（FR-1.5）。

两层都**不依赖 FastAPI 上下文**，可脱离 Web 层独立单测；都不直接写数据库
判定逻辑（分层约束，§7.1）。
"""

from __future__ import annotations