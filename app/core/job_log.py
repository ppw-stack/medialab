"""作业执行日志采集（调度作业的时序日志）。

背景：``job_runs.result_json`` 里只有 ``summary`` + ``steps``——
**没有时间线**。用户排查时知道「结果是什么」，但不知道
「哪一步慢」「中间发生了什么」「失败前最后说了什么」。

本模块在作业执行期间挂一个 :class:`logging.Handler` 到 root logger，
把该作业期间的 Python 日志收集成**结构化**条目，随作业结果一起落库。

**并发隔离**（关键）：多个作业可能并发跑（如 ``fetch_all_sites``
与 ``push_and_monitor_free`` 同时触发），日志必须**各归各的**。
做法是 :class:`contextvars.ContextVar` 记录「当前采集器」——
``emit`` 只接受**自己**发来的记录，别的作业的日志直接忽略。

    asyncio.gather 派生的 task 会**继承** context，故 ``user_data``
    那种「并发抓 9 站」的子任务日志也能收全。

**不影响原有日志**：采集器只是 root logger 上的**额外** handler，
原有 stdout / 文件 handler 照常工作（``propagate`` 保持 True）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.core.timeutils import bj_now

logger = get_logger(__name__)

#: 单次作业最多保留的日志条数（超出丢最旧的，保留最新的）。
MAX_LOG_ENTRIES = 200


@dataclass(slots=True)
class JobLogEntry:
    """一条作业日志（结构化，JSON 可序列化）。"""

    #: 北京时间字符串（``yyyy-MM-dd HH:mm:ss``）
    ts: str
    #: 日志级别名（INFO / WARNING / ERROR …）
    level: str
    #: 发出日志的 logger 名（如 ``app.services.jobs``）
    logger: str
    #: 日志正文
    message: str


class JobLogCollector(logging.Handler):
    """收集作业执行期间的 Python 日志。

    典型用法见 :func:`capture_job_logs`。
    """

    def __init__(self) -> None:
        # level=NOTSET：**不额外过滤**——收下所有到达 root handler 的记录。
        #
        # 真正的门槛是 **root logger 的级别**（``LOG_LEVEL`` 配置）：
        # ``logger.info()`` 在 root 级别是 WARNING 时压根不产生 record，
        # handler 也就无从收到。生产默认 ``LOG_LEVEL=INFO`` → 完整时间线；
        # 运维调成 WARNING → 作业日志只剩警告以上（与容器日志一致）。
        super().__init__(level=logging.NOTSET)
        self._entries: list[JobLogEntry] = []
        self._truncated = 0

    @property
    def truncated(self) -> int:
        """因超限被丢弃的条数（0 = 没丢过）。"""
        return self._truncated

    def emit(self, record: logging.LogRecord) -> None:
        """收下一条日志。

        只收**本作业**的——并发隔离靠这一步。
        """
        # ContextVar 指向「当前作业的采集器」：别的作业发来的日志
        # ctx.get() is not self → 直接忽略，不会串到本作业的记录里。
        if _current_collector.get() is not self:
            return
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 参数格式化异常极罕见
            message = str(record.msg)
        if record.exc_info:
            # 异常堆栈不能丢——失败作业的堆栈是最有价值的排障信息
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        self._entries.append(
            JobLogEntry(
                ts=bj_now(),
                level=record.levelname,
                logger=record.name,
                message=message,
            )
        )
        if len(self._entries) > MAX_LOG_ENTRIES:
            # 丢**最旧**的：排查时最新几条最关键
            del self._entries[0]
            self._truncated += 1

    def entries(self) -> list[dict[str, Any]]:
        """导出结构化条目（JSON 可序列化）。"""
        return [
            {
                "ts": e.ts,
                "level": e.level,
                "logger": e.logger,
                "message": e.message,
            }
            for e in self._entries
        ]


#: 当前生效的采集器（None = 没在采集）。
#:
#: **ContextVar 而非普通全局变量**：asyncio 的每个 task 有独立 context，
#: 普通全局变量会让并发作业互相覆盖采集器。
_current_collector: ContextVar[JobLogCollector | None] = ContextVar(
    "job_log_collector", default=None
)


@contextmanager
def capture_job_logs(
    collector: JobLogCollector | None = None,
) -> Iterator[JobLogCollector]:
    """采集上下文内的日志。

    :param collector: 外部创建的采集器——**作业抛异常时也要能拿到已
        收集的日志**，故调用方通常自己创建并在 ``finally`` 里读。
        None = 内部新建（此时异常路径拿不到已收集的条目）。
    """
    col = collector if collector is not None else JobLogCollector()
    root = logging.getLogger()
    token = _current_collector.set(col)
    root.addHandler(col)
    try:
        yield col
    finally:
        # removeHandler 对不存在的 handler 是安全的（内部吞 ValueError），
        # 故 configure_logging 中途重置 handler 也不会在这里炸。
        root.removeHandler(col)
        _current_collector.reset(token)


__all__ = [
    "MAX_LOG_ENTRIES",
    "JobLogCollector",
    "JobLogEntry",
    "capture_job_logs",
]
