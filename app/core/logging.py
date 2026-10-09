"""结构化日志（FR-8.2 / FR-2.6）。

- 文本模式：人读，生产排障用
- JSON 模式：机器读，容器日志采集用（``LOG_JSON=true``）

所有对下载器的**写操作**必须经 :func:`audit` 落审计（FR-2.6），审计与业务日志
分离，便于面板按时间轴检索与导出。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any, Final

_RESERVED: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"asctime", "message", "taskName"}


class JsonFormatter(logging.Formatter):
    """把 extra 字段并入 JSON 输出。"""

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
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """幂等配置根 logger。"""
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

    # 第三方库降噪：正常业务不依赖其 INFO 输出
    for noisy in ("apscheduler.executors.default", "httpx", "httpcore", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def audit(
    action: str,
    *,
    downloader: str | None = None,
    source: str = "schedule",
    count: int | None = None,
    reason: str | None = None,
    logger: logging.Logger | None = None,
) -> Iterator[MutableMapping[str, Any]]:
    """下载器写操作审计（FR-2.6）。

    用法::

        with audit("remove", downloader="qbt-main", reason="window_expired") as ctx:
            ctx["hash_count"] = len(hashes)

    ``source`` 为原因触发源：``schedule`` / ``manual`` / ``alert_linked`` /
    ``window_abort`` / ``rule``。
    """
    log = logger or get_logger("seedhelper.audit")
    extra: dict[str, Any] = {
        "action": action,
        "downloader": downloader,
        "source": source,
        "audit": True,
    }
    if reason:
        extra["reason"] = reason
    log.info("操作开始 action=%s", action, extra=extra)
    ctx: MutableMapping[str, Any] = dict(extra)
    ctx["hash_count"] = count
    try:
        yield ctx
    except Exception as exc:
        log.error(
            "操作失败 action=%s error=%s",
            action,
            type(exc).__name__,
            extra={**extra, "result": "error", "error_type": type(exc).__name__},
            exc_info=True,
        )
        raise
    else:
        if ctx.get("hash_count") is not None:
            extra["hash_count"] = ctx["hash_count"]
        log.info("操作成功 action=%s", action, extra={**extra, "result": "ok"})