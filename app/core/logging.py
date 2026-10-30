"""结构化日志（FR-8.2）。

- 文本模式：人读，生产排障用
- JSON 模式：机器读，容器日志采集用（``LOG_JSON=true``）
"""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Final

_RESERVED: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message", "taskName"}

#: 任何 key 命中（大小写不敏感）都脱敏为占位符
_SECRET_KEYS: Final = frozenset({
    "password",
    "token",
    "cookie",
    "cookies",
    "apikey",
    "authorization",
    "secret",
    "rss_url",
    "session",
})


def _scrub(value: Any) -> Any:
    """递归清洗 dict/list/tuple 中的敏感字段。"""
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in _SECRET_KEYS else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(x) for x in value]
    return value


class JsonFormatter(logging.Formatter):
    """把 extra 字段并入 JSON 输出，并对敏感字段脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _scrub(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = False,
    log_dir: Path | None = None,
) -> None:
    """幂等配置根 logger。

    Args:
        level: 日志级别。
        json_output: 是否输出 JSON（容器环境便于采集）。
        log_dir: 若提供，**额外**写一份到 ``{log_dir}/medialab.log``
            （按大小轮转）。容器里方便 ``tail -f`` 回溯问题。
            文件里永远用**人类可读**格式（JSON 是给采集器看的）。
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_output else logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "medialab.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            # 文件里永远用人类可读格式（JSON 是给采集器看的）
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - 目录不可写时降级
            root.warning("无法写日志文件（仅输出到 stdout）：%s", exc)

    # 第三方库降噪：正常业务不依赖其 INFO 输出
    for noisy in (
        "apscheduler.executors.default",
        "httpx",
        "httpcore",
        "aiosqlite",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)